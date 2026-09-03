"""Distributed tracing, and the strict limits on what a trace may say.

ADR-083 records the decisions; this is where they are enforced.

A request that arrives at the API, is stored, is queued, is picked up minutes
later by a different process, is answered by OpenAI and is sent back through
Meta is one piece of work in five places. Logs correlate the first leg by
`request_id` and stop at the queue, because the worker leg is a different
process reading a Redis list. Tracing is what carries the thread across that
boundary.

What is traced, and nothing else
--------------------------------

Four span kinds, written by hand:

- ``SERVER``  — one per HTTP request, named for the *route template*.
- ``PRODUCER`` — one per job put on a queue, carrying the trace context.
- ``CONSUMER`` — one per attempt at a queued job, continuing that trace.
- ``CLIENT``  — one per call to an external provider.

Plus an ``INTERNAL`` span around a database unit of work, which is what makes
the P2-B property legible: the database spans of an agent turn *end* before the
provider span begins, so a reader can see the connection was released rather
than take it on faith.

**No auto-instrumentation.** Not `opentelemetry-instrumentation-fastapi`, not
`-httpx`, not `-sqlalchemy`. Each of them is one configuration flag away from
exporting exactly what this application spends the rest of its code keeping
out of logs: FastAPI's records the requested path and query string (which carry
conversation, lead and media identifiers), httpx's records full request URLs
(Paymob, S3, Google's token endpoint), SQLAlchemy's records statement text. In
every case the privacy control would be a setting somebody could change, in a
package this repository does not own. Written by hand, the attribute set is an
allowlist by construction, and `tests/unit/test_trace_privacy.py` holds it.

**No inbound trace context from HTTP.** Every API request starts a new trace.
Wasla's HTTP callers are a browser frontend and Meta, Paymob and Resend
webhooks; none of them participates in Wasla's traces, and all of them are
outside the trust boundary. Honouring a `traceparent` from the internet would
let a stranger choose trace identifiers, merge unrelated requests into one
trace, and write up to 512 bytes of `tracestate` into every span a request
produces. The propagation that actually matters — API to worker, across the
queue — is entirely internal. If a trusted upstream service ever exists, this
is where extraction would be added, gated on `trusted_proxy_ips` the way
`SecurityHeadersMiddleware` gates the forwarded protocol.

**No exception text.** `record_exception=False` and
`set_status_on_exception=False` on every span this module opens, because both
default to true and both put `str(exception)` into the exported span — a
provider's error body, a database error quoting a parameter, a validation
message quoting the value that failed. What is recorded instead is the
exception's *class name*, which is a code-defined identifier and cannot carry
customer data.

Failure is never the work's problem
-----------------------------------

Disabled is the default, and disabled costs nothing: with no provider
installed the API package's no-op tracer answers every call, so the spans
below compile to a few attribute lookups.

Enabled, exports run on `BatchSpanProcessor`'s own thread. A collector that is
down, slow or refusing loses spans and logs about it; it cannot fail a request,
fail a job, retry a payment or change any outcome. That is asserted rather than
assumed — see `tests/integration/test_trace_isolation.py`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import NoOpTracer, Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The instrumentation scope every span in this application is created under.
INSTRUMENTATION_NAME: Final = "wasla"

#: What a deployment calls each process, when nothing else is configured.
#: Not the worker *kind*: one worker process runs up to nine loops, so naming
#: the service after one of them would be a lie in eight cases. Which loop a
#: span belongs to is an attribute on the span (`wasla.queue`).
API_SERVICE_NAME: Final = "wasla-api"
WORKER_SERVICE_NAME: Final = "wasla-worker"

#: The only carrier keys that cross the queue. W3C trace context and nothing
#: else — not a header bag, not a baggage payload, not an application field.
TRACEPARENT: Final = "traceparent"
TRACESTATE: Final = "tracestate"
TRACE_CARRIER_KEYS: Final[frozenset[str]] = frozenset({TRACEPARENT, TRACESTATE})

#: `00-<32 hex>-<16 hex>-<2 hex>` is 55 characters; the spec allows a longer
#: form for future versions, so this leaves a little room and refuses anything
#: that is plainly not a traceparent.
MAX_TRACEPARENT_LENGTH: Final = 64
#: The W3C limit. A carrier is read back out of Redis, so the bound is what
#: stops a malformed or hostile entry making every job envelope large.
MAX_TRACESTATE_LENGTH: Final = 512

# ------------------------------------------------------------- attribute names
#
# The complete allowlist. Anything not on this list does not go on a span, and
# the reason each one is safe is that its domain is fixed by this repository's
# own code: a route template, an HTTP method, a queue name, a provider name, an
# operation constant, a small integer. None of them grows with the traffic and
# none of them can carry what a customer typed.

HTTP_METHOD: Final = "http.request.method"
HTTP_ROUTE: Final = "http.route"
HTTP_STATUS: Final = "http.response.status_code"

QUEUE: Final = "wasla.queue"
JOB_ATTEMPT: Final = "wasla.job_attempt"
JOB_OUTCOME: Final = "wasla.job_outcome"

PROVIDER: Final = "wasla.provider"
PROVIDER_OPERATION: Final = "wasla.provider_operation"
PROVIDER_OUTCOME: Final = "wasla.provider_outcome"

DB_SYSTEM: Final = "db.system"
DB_SYSTEM_VALUE: Final = "postgresql"

ALLOWED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        HTTP_METHOD,
        HTTP_ROUTE,
        HTTP_STATUS,
        QUEUE,
        JOB_ATTEMPT,
        JOB_OUTCOME,
        PROVIDER,
        PROVIDER_OPERATION,
        PROVIDER_OUTCOME,
        DB_SYSTEM,
    }
)


class TracingConfigurationError(RuntimeError):
    """Tracing was switched on without what it needs to export anything.

    Raised at start-up rather than warned about. A deployment that asked for
    tracing and silently got none is worse than one that refuses to boot: the
    absence is discovered during the incident the traces were for.
    """


_provider: TracerProvider | None = None
_NO_OP: Final = NoOpTracer()
_PROPAGATOR: Final = TraceContextTextMapPropagator()


def tracer() -> Tracer:
    """The tracer, or a no-op when tracing is not configured in this process.

    Read through a module-level provider rather than
    `opentelemetry.trace.get_tracer()` so that configuring twice — which a test
    does constantly and the global API refuses with a warning — is an ordinary
    operation.
    """
    if _provider is None:
        return _NO_OP
    return _provider.get_tracer(INSTRUMENTATION_NAME)


def tracing_enabled() -> bool:
    return _provider is not None


def configure_tracing(settings: Settings, *, service_name: str) -> None:
    """Install the SDK for this process, if the deployment asked for it.

    Called once per process, from the API's lifespan and the worker's entry
    point. Does nothing when tracing is off, which is the default: no exporter
    is built, no thread is started, and no network destination is required for
    the process to serve traffic.
    """
    global _provider
    if not settings.tracing_enabled:
        _provider = None
        return

    endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
    if not endpoint:
        raise TracingConfigurationError(
            "TRACING_ENABLED is true but OTEL_EXPORTER_OTLP_ENDPOINT is not set; "
            "tracing cannot export anywhere"
        )

    # Imported here rather than at module scope so that a deployment which does
    # not trace never loads the exporter, its protobuf machinery or its HTTP
    # client. Tracing being off should cost nothing, including at import.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter: SpanExporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    install_tracing(
        exporter,
        service_name=settings.otel_service_name or service_name,
        sample_ratio=settings.otel_traces_sampler_arg,
    )
    logger.info(
        "tracing.enabled",
        extra={
            "event": "tracing.enabled",
            "service_name": settings.otel_service_name or service_name,
            "sample_ratio": settings.otel_traces_sampler_arg,
        },
    )


def install_tracing(
    exporter: SpanExporter,
    *,
    service_name: str,
    sample_ratio: float = 1.0,
) -> TracerProvider:
    """Point this process's spans at an exporter. Also the test seam.

    `ParentBased(TraceIdRatioBased(...))` rather than a bare ratio, so a
    sampling decision is made once per trace and honoured by everything
    downstream. Without it the API could sample a request in and the worker
    sample the same trace out, which produces a trace that is missing its
    middle — the least useful possible outcome of sampling.

    The sampling ratio is deliberately not a function of the tenant, the route
    or the payment: a deployment that traced payments at a different rate from
    everything else would have two populations in one metric and no way to tell
    which it was looking at.
    """
    global _provider
    _provider = TracerProvider(
        # `Resource.create` adds `service.name`, a per-process
        # `service.instance.id` and the SDK's own name and version. It adds no
        # hostname, no IP address and no process arguments - the detectors that
        # would are opt-in and are deliberately not opted into, because a trace
        # backend is a third party and a deployment's topology is not something
        # to hand one by default.
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    return _provider


def shutdown_tracing() -> None:
    """Flush and stop. Safe to call when nothing was ever configured."""
    global _provider
    if _provider is None:
        return
    provider, _provider = _provider, None
    try:
        provider.shutdown()
    except Exception:
        # A collector that will not take the last batch on the way out is not
        # a reason to fail a shutdown that has already stopped serving.
        logger.warning("tracing.shutdown_failed", extra={"event": "tracing.shutdown_failed"})


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, str | int | float | bool] | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """Open a span, and never let it say more than it should.

    `record_exception` and `set_status_on_exception` are both switched off, and
    that is the whole privacy argument for this function existing rather than
    call sites using the SDK directly. Left at their defaults, an exception
    leaving this block would export `str(exception)` as a status description
    and a full stack trace as a span event. This records the exception's class
    name and nothing else.
    """
    with tracer().start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=dict(attributes) if attributes else None,
        record_exception=False,
        set_status_on_exception=False,
    ) as active:
        try:
            yield active
        except BaseException as error:
            active.set_status(Status(StatusCode.ERROR, type(error).__name__))
            raise


def record_span(
    name: str,
    *,
    kind: SpanKind,
    started_at_ns: int,
    attributes: Mapping[str, str | int | float | bool] | None = None,
    error: str | None = None,
) -> None:
    """Write a span for work that has already finished.

    For a boundary whose start and end are known but which is not shaped like a
    block — `ProviderCall`, where the clock starts at the top of a client
    method and the outcome is decided at one of a dozen exits. Creating and
    ending the span in one statement means there is no path on which a span is
    opened and never closed, which is the failure mode a context manager around
    a retry loop would have to be audited for at every exit.

    Nothing can be a child of a span recorded this way, which is correct here:
    no Wasla code runs inside a provider call.
    """
    active = tracer().start_span(
        name,
        kind=kind,
        start_time=started_at_ns,
        attributes=dict(attributes) if attributes else None,
        record_exception=False,
        set_status_on_exception=False,
    )
    if error is not None:
        active.set_status(Status(StatusCode.ERROR, error))
    active.end(end_time=time.time_ns())


def carrier() -> dict[str, str]:
    """W3C trace context for whatever span is active, or an empty carrier.

    Empty is the ordinary answer when tracing is off, and the queue treats it
    as "no context" rather than as an error - which is what keeps tracing out
    of the correctness path.
    """
    out: dict[str, str] = {}
    _PROPAGATOR.inject(out)
    return {key: value for key, value in out.items() if key in TRACE_CARRIER_KEYS}


def sanitise_carrier(raw: object) -> dict[str, str]:
    """The trace context worth storing, out of whatever was handed over.

    Everything that is not a short string under one of the two W3C keys is
    dropped. This runs on the way *in* to a job envelope and on the way back
    out of it, because a queue entry is data from another process and an older
    release's - and a carrier is the one field in the envelope that is not
    produced by the job's own encoder.

    Never raises. A carrier this cannot make sense of is no carrier, which
    starts a new trace rather than refusing to run the job.
    """
    if not isinstance(raw, Mapping):
        return {}
    limits = {TRACEPARENT: MAX_TRACEPARENT_LENGTH, TRACESTATE: MAX_TRACESTATE_LENGTH}
    cleaned: dict[str, str] = {}
    for key, limit in limits.items():
        value = raw.get(key)
        if isinstance(value, str) and value and len(value) <= limit:
            cleaned[key] = value
    # `tracestate` without `traceparent` names no span, so it is not context.
    return cleaned if TRACEPARENT in cleaned else {}


def context_from(raw: object) -> Context | None:
    """The parent context a carrier names, or `None` for "start a new trace".

    A malformed `traceparent` is not an error here and must never become one:
    the propagator returns a context with no span in it, this returns `None`,
    and the job runs under a fresh trace. A queue entry written by an older
    release, a truncated value, a hostile one - all of them mean the same
    thing operationally, which is that this attempt is the start of its own
    story.
    """
    cleaned = sanitise_carrier(raw)
    if not cleaned:
        return None
    extracted: Context = _PROPAGATOR.extract(cleaned)
    return extracted if trace.get_current_span(extracted).get_span_context().is_valid else None


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "API_SERVICE_NAME",
    "DB_SYSTEM",
    "DB_SYSTEM_VALUE",
    "HTTP_METHOD",
    "HTTP_ROUTE",
    "HTTP_STATUS",
    "INSTRUMENTATION_NAME",
    "JOB_ATTEMPT",
    "JOB_OUTCOME",
    "MAX_TRACEPARENT_LENGTH",
    "MAX_TRACESTATE_LENGTH",
    "PROVIDER",
    "PROVIDER_OPERATION",
    "PROVIDER_OUTCOME",
    "QUEUE",
    "TRACEPARENT",
    "TRACESTATE",
    "TRACE_CARRIER_KEYS",
    "WORKER_SERVICE_NAME",
    "SpanKind",
    "TracingConfigurationError",
    "carrier",
    "configure_tracing",
    "context_from",
    "install_tracing",
    "record_span",
    "sanitise_carrier",
    "shutdown_tracing",
    "span",
    "tracer",
    "tracing_enabled",
]
