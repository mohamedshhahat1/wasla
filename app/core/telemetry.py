"""Every operational signal this application emits, and where each one lives.

Two mechanisms, and which one a signal uses follows from one question: does
the process that produces it also serve HTTP?

**In-process counters, for the API's own request path.** HTTP rate, latency
and dependency health are recorded in `app.core.metrics.REGISTRY` and read
straight off it. Each API replica is its own scrape target, which is the
ordinary Prometheus model, and a request must not pay a Redis round trip to be
counted.

**Redis counters, for everything the worker produces.** The worker deliberately
serves no HTTP — its health probe is a *command* for exactly that reason
(`app/workers/health.py`) — so giving it a metrics listener would hand it an
attack surface it does not currently have, on a container that holds the Meta
token and the OpenAI key. Redis is already the cross-process channel for
heartbeats and queues, so job outcomes and provider calls go there and the API
renders them at scrape time (ADR-069). A provider call already costs an HTTP
request to somebody else; one `HINCRBY` beside it is not the expensive part.

**Nothing here may fail a request.** Every public function in this module
swallows. A metric is an observation of the work, never a participant in it, so
a Redis outage, a label mistake or a full keyspace loses a sample and changes
nothing else. `app.core.metrics` raises so the guards are testable; this module
is where that raising stops.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter, time_ns
from typing import Any, Final, cast

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.metrics import PROVIDER_LATENCY_BUCKETS, REGISTRY, HistogramSample, bucket_for
from app.core.tracing import (
    PROVIDER,
    PROVIDER_OPERATION,
    PROVIDER_OUTCOME,
    SpanKind,
    record_span,
)

logger = get_logger(__name__)

# One Redis hash per metric, field per label combination. A hash rather than a
# key per series keeps the keyspace at one entry per metric no matter how the
# labels multiply, and makes a scrape one `HGETALL` instead of a scan.
COUNTER_PREFIX: Final = "metrics:counter"
# The same arrangement for a distribution: one hash per metric, and one field
# per (label combination, bucket). See `_observe` for why an observation
# touches two fields rather than eleven.
HISTOGRAM_PREFIX: Final = "metrics:histogram"
# What separates a label combination from the part of the field that says which
# bucket. Chosen because no label value in this application contains it: every
# one is an enum member, a short operation constant or a process role.
BUCKET_SEPARATOR: Final = "|"
# The field holding the sum of every observation for one label combination.
SUM_FIELD: Final = "sum"


class Provider(StrEnum):
    """The external systems worth counting separately."""

    OPENAI = "openai"
    WHATSAPP = "whatsapp"
    PAYMOB = "paymob"
    EMAIL = "email"


class CallOutcome(StrEnum):
    """How a call to somebody else ended.

    Four values, and no more. The provider's own error catalogue is not a
    label domain — it is unbounded, it changes without notice, and its text
    can echo the request. `failure` plus a log line is the honest pairing.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


class JobOutcome(StrEnum):
    """How one attempt at a queued job ended.

    The last two are the crash-recovery pair, and they are separate because an
    operator reads them differently: `recovered` is the system healing itself
    after a worker died, and `quarantined` is a job it refused to heal because
    doing so might send somebody a second message.
    """

    SUCCEEDED = "succeeded"
    RETRIED = "retried"
    DEAD_LETTERED = "dead_lettered"
    RECOVERED = "recovered"
    QUARANTINED = "quarantined"


# ------------------------------------------------------ in-process (API only)

HTTP_REQUESTS = REGISTRY.counter(
    "wasla_http_requests_total",
    "HTTP requests served, by method, route template and status class.",
    ("method", "route", "status"),
)

HTTP_LATENCY = REGISTRY.histogram(
    "wasla_http_request_duration_seconds",
    "How long a handler took, by method and route template.",
    ("method", "route"),
)

HTTP_IN_FLIGHT = REGISTRY.gauge(
    "wasla_http_requests_in_flight",
    "Requests currently being handled by this process.",
)

DEPENDENCY_UP = REGISTRY.gauge(
    "wasla_dependency_up",
    "Whether a readiness dependency answered its last probe (1) or did not (0).",
    ("dependency",),
)

DEPENDENCY_FAILURES = REGISTRY.counter(
    "wasla_dependency_check_failures_total",
    "Readiness probes that found a dependency unavailable.",
    ("dependency",),
)

UNHANDLED_ERRORS = REGISTRY.counter(
    "wasla_unhandled_errors_total",
    "Exceptions that reached the last-resort handler and became a 500.",
)

AUTH_SECURITY_EVENTS = REGISTRY.counter(
    "wasla_auth_security_events_total",
    "Authentication and account-security outcomes with bounded privacy-safe labels.",
    ("event", "outcome", "reason"),
)


LIFECYCLE_EVENTS = REGISTRY.counter(
    "wasla_lifecycle_operations_total",
    "Account and workspace lifecycle operations by outcome.",
    ("operation", "outcome"),
)


def observe_http(*, method: str, route: str, status_code: int, duration_seconds: float) -> None:
    """Record one served request.

    `route` must be the matched route *template* — `/api/v1/leads/{lead_id}` —
    never the requested path, or every identifier in every URL becomes its own
    time series. The middleware reads it off the resolved route for exactly
    that reason, and the label guard refuses a UUID if it ever stops.
    """
    status_class = f"{status_code // 100}xx"
    try:
        HTTP_REQUESTS.increment(method=method, route=route, status=status_class)
        HTTP_LATENCY.observe(duration_seconds, method=method, route=route)
    except Exception:
        logger.warning("metrics.record_failed", extra={"event": "metrics.record_failed"})


def observe_dependency(name: str, *, healthy: bool) -> None:
    try:
        DEPENDENCY_UP.set(1.0 if healthy else 0.0, dependency=name)
        if not healthy:
            DEPENDENCY_FAILURES.increment(dependency=name)
    except Exception:
        logger.warning("metrics.record_failed", extra={"event": "metrics.record_failed"})


def observe_unhandled_error() -> None:
    # A counter with no labels has nothing to reject, so this cannot realistically
    # fail - but it runs on the path that is already handling an exception, and
    # raising a second one there would replace the error an operator needs to see.
    with contextlib.suppress(Exception):
        UNHANDLED_ERRORS.increment()


def observe_auth_event(*, event: str, outcome: str, reason: str) -> None:
    """Count one auth/security event without identities or caller input."""
    try:
        AUTH_SECURITY_EVENTS.increment(event=event, outcome=outcome, reason=reason)
    except Exception:
        logger.warning("metrics.record_failed", extra={"event": "metrics.record_failed"})


def observe_lifecycle_event(*, operation: str, outcome: str) -> None:
    """Count one account or workspace lifecycle operation.

    **Both labels come from module constants, never from a request.** The
    operation is one of a fixed list written down at the call sites, and the
    outcome is one of three words. That is the whole cardinality of this metric
    and it cannot grow with traffic - no user id, no tenant id, no slug, no
    email. A workspace identifier here would mean a time series per customer,
    which is how a metrics backend is taken down by a successful product.

    Recorded for the *decided* outcomes only. A refusal the caller caused is
    `conflict`; work that completed is `success`; an unexpected failure is
    `error`, and is also already counted by `wasla_unhandled_errors_total` and
    by the 5xx class on `wasla_http_requests_total` - which is what an alert on
    "the lifecycle endpoints are failing" should actually fire on, because it
    catches a failure that never reached a call site here at all.
    """
    try:
        LIFECYCLE_EVENTS.increment(operation=operation, outcome=outcome)
    except Exception:
        logger.warning("metrics.record_failed", extra={"event": "metrics.record_failed"})


# ------------------------------------------------- cross-process (via Redis)

# Declared here so the scrape knows what to read and the exposition can carry
# HELP text for a metric this process never increments itself.
REDIS_COUNTERS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "wasla_jobs_total": (
        "Queued jobs by how the attempt ended.",
        ("queue", "outcome"),
    ),
    "wasla_job_failures_total": (
        "Failed job attempts by failure category.",
        ("queue", "category"),
    ),
    "wasla_provider_requests_total": (
        "Calls to an external provider, by provider, operation and outcome.",
        ("provider", "operation", "outcome"),
    ),
    # Every HTTP attempt inside a provider call, retries included (AI-09). The
    # counter above records one final outcome per call, so a call that took two
    # 429s and then succeeded read as one success, and throttling the retry had
    # absorbed was invisible - exactly what a rate-limit alert exists to catch.
    # The same labels and the same four-value outcome domain.
    "wasla_provider_attempts_total": (
        "Individual attempts at an external provider, retries included, by outcome.",
        ("provider", "operation", "outcome"),
    ),
    # How agent turns ended (AI-09). `outcome` is one of `TurnOutcome`'s eleven
    # fixed values and nothing else: no workspace, conversation or message id,
    # so the cardinality of this metric is eleven for ever. The silent endings
    # the AI audit found - an allowance refused, an empty answer, a reply
    # suppressed - each have a series here instead of a warning line.
    "wasla_agent_turn_outcomes_total": (
        "Agent turns by how they ended.",
        ("outcome",),
    ),
    # What the tool executor did, by tool and by closed outcome (TOOL-11).
    # There was no tool series at all: a crashed turn caused by a tool, an
    # authorization denial and a duplicate suppression were visible only as a
    # generic worker failure or a log line, so a grant being abused or one
    # workspace's tool failing could not be seen, graphed or alerted on.
    #
    # Both labels are closed. `tool` is a name from the deployment's own
    # registry - four values today, and a name the model invented is counted as
    # `unknown` rather than becoming a label of its own, because a label domain
    # a stranger's message can extend is a cardinality leak. `outcome` is the
    # six terminal states. No tenant, conversation, turn or call id appears
    # anywhere near this metric.
    "wasla_agent_tool_executions_total": (
        "Agent tool calls by tool and by how the execution ended.",
        ("tool", "outcome"),
    ),
    # Retention (ADR-078). One label with three fixed values, and no tenant,
    # media id, filename or storage key anywhere near it - the cardinality of
    # this metric is three, for ever.
    #
    # `pending` is the one worth alerting on. `purged` and `failed` are rates
    # and a bad day in either is visible; a store that has been refusing
    # deletions is invisible in both, because the rows are claimed, the sweep
    # reports itself as having run, and the volume simply does not shrink.
    "wasla_media_retention_total": (
        "Stored files the retention sweep removed, failed to remove, or is still holding.",
        ("outcome",),
    ),
    # Upload reconciliation (ADR-087). One label again, six fixed values, and
    # nothing that identifies a workspace, a file or an object: no tenant, no
    # media id, no storage key, no filename, no hash, no bucket. The cardinality
    # of this metric is six, for ever.
    #
    # `mismatched` is the one that means a person is needed - an object at a key
    # Wasla owns whose contents are not what Wasla wrote - and it is expected to
    # be zero always rather than usually. `pending` is the level to watch: a
    # number that does not come back down across passes is a store accepting
    # neither writes nor questions about them.
    "wasla_media_upload_reconciliation_total": (
        "Interrupted object writes by how reconciliation settled them.",
        ("outcome",),
    ),
    # Payment reconciliation (ADR-088), and the compensating control that ADR
    # names for an accepted risk: an attempt that can never be resolved keeps a
    # workspace served indefinitely, and that is only acceptable because the
    # backlog is alertable on its age. It was written to Redis and absent from
    # this dictionary, so the hash accumulated and the scrape never read it -
    # while `docs/OBSERVABILITY.md`, `docs/BILLING.md` and `docs/RUNBOOK.md` all
    # told operators to watch it.
    #
    # One label, seven fixed values, and no identifier among them: no workspace,
    # no invoice, no payment, no provider reference, no amount. `pending` is the
    # level to watch and `unreachable` is the one that must never be read as
    # `not_found` - see the reconciler. The age of the oldest unresolved attempt
    # is the histogram beside this, because summing ages is meaningless.
    "wasla_payment_reconciliation_total": (
        "Unresolved collection attempts by how reconciliation settled them.",
        ("outcome",),
    ),
    # Commercial billing outcomes (BILL-14). Before these, a checkout started, a
    # payment that succeeded, a renewal that failed and a callback that was
    # refused were each a log line and nothing an alert could fire on. Every
    # label is one closed word from `BILLING_OUTCOMES` - never a workspace, an
    # invoice, a payment, a provider reference, an amount or an e-mail - so the
    # cardinality of each series is fixed for ever.
    "wasla_billing_checkouts_total": (
        "Hosted checkouts by lifecycle step: created, settled, failed.",
        ("outcome",),
    ),
    "wasla_billing_payments_total": (
        "Payment outcomes as settlement decided them.",
        ("outcome",),
    ),
    "wasla_billing_renewals_total": (
        "Automatic renewal steps by outcome.",
        ("outcome",),
    ),
    "wasla_billing_callbacks_total": (
        "Payment provider callbacks by what settlement did with them.",
        ("outcome",),
    ),
    "wasla_billing_refunds_total": (
        "Refunds by stage: requested, refused, confirmed, review_requested.",
        ("outcome",),
    ),
    "wasla_billing_hosted_reconciliation_total": (
        "Pending hosted-checkout payments by what a provider inquiry found.",
        ("outcome",),
    ),
    # Every durable billing incident as it is raised: duplicate payments,
    # mismatched and unknown callbacks, permanent provider refusals. `kind` is
    # `BillingIncidentKind`, seven values.
    "wasla_billing_incidents_total": (
        "Billing incidents raised, by kind.",
        ("kind",),
    ),
    # How ingestion jobs ended (RAG-05). `outcome` is one of `IndexingOutcome`'s
    # seven fixed values - published, retry scheduled, failed, exhausted, stale,
    # suspended, skipped - and nothing identifying: no workspace, no document,
    # no failure code. Which document failed and why is on its generation row.
    "wasla_rag_ingestion_outcomes_total": (
        "Document indexing jobs by how they ended.",
        ("outcome",),
    ),
    # Knowledge searches by what they found (RAG-05). Three values: `found`,
    # `empty`, `failed`. `failed` is the one that used to be invisible - an
    # embedding outage made every agent say it had no information, with every
    # alert green - and `empty` is kept apart from it because a knowledge base
    # with nothing on a subject is an answer, not an incident.
    "wasla_rag_retrievals_total": (
        "Knowledge searches by outcome.",
        ("outcome",),
    ),
    # How each inbound attachment ended (MEDIA-15). `outcome` is `ready` or one
    # of the media reason tokens - a closed vocabulary of about two dozen,
    # enforced by `MEDIA_OUTCOMES` below - and nothing identifying: no
    # workspace, file, key, URL or filename. The download failure classes are
    # the subset `unavailable`, `credential_refused`, `credential_unavailable`,
    # `host_refused`, `malformed_descriptor`, `rate_limited`,
    # `download_failed` and `timeout`.
    "wasla_media_outcomes_total": (
        "Inbound attachments by how processing ended.",
        ("outcome",),
    ),
    # What the media recovery sweep did with files no attempt was finishing
    # (MEDIA-03): put back on the queue, given up on, an owed agent turn the
    # queue refused (still owed, retried), or an owed turn republished after
    # nobody took it up. Four values.
    "wasla_media_recovery_total": (
        "Stranded attachments and owed agent turns the media recovery sweep handled.",
        ("outcome",),
    ),
    # Object deletes a workspace purge owed, by how the attempt went
    # (MEDIA-07). `failed` is a store refusing; the owed level is the gauge
    # `wasla_media_purge_deletes_owed`.
    "wasla_media_purge_objects_total": (
        "Purged workspaces' object deletes, by whether the store accepted them.",
        ("outcome",),
    ),
}

# The closed label domain of `wasla_media_outcomes_total`: `ready` and the
# media reason tokens (`app.services.media_outcomes.MediaReason`), restated here
# because `core` imports nothing from `services`; a test holds the two equal.
# Anything else is counted as `unknown`, so no caller can widen the domain.
MEDIA_OUTCOMES: Final = frozenset(
    {
        "ready",
        "no_file",
        "oversize",
        "unsupported_type",
        "type_mismatch",
        "capacity",
        "unavailable",
        "nothing_stored",
        "unreadable",
        "workspace_suspended",
        "workspace_deleted",
        "channel_unavailable",
        "credential_refused",
        "credential_unavailable",
        "host_refused",
        "malformed_descriptor",
        "rate_limited",
        "download_failed",
        "timeout",
        "storage_failed",
        "upload_conflict",
        "provider_failed",
        "reader_failed",
        "abandoned",
    }
)

# Distributions written across processes, by metric name: help text, the labels
# a sample must carry, and the bucket bounds this release declares.
#
# One entry, and the labels are deliberately the *pair* the counter beside it
# already uses minus the outcome. A duration is recorded whether the call
# succeeded or failed - a provider timing out after twenty seconds is the most
# important latency this metric can hold - and splitting the distribution by
# outcome would quadruple the series to answer a question `..._requests_total`
# already answers. An operator asks "how slow is OpenAI" and "how often does it
# fail" separately, and gets each from the metric shaped for it.
# How old the oldest unanswered collection attempt is when a reconciliation
# pass runs (ADR-088). Spread over minutes to days rather than the sub-second
# spacing latency wants: everything under five minutes is inside the grace
# period and uninteresting, and the buckets an operator actually alerts on are
# the last two - an attempt outstanding for an hour means callbacks are not
# arriving, and one outstanding for a day means an invoice nobody can collect
# and possibly a customer who has already paid.
PENDING_PAYMENT_AGE_BUCKETS: Final[tuple[float, ...]] = (
    300.0,
    900.0,
    3_600.0,
    21_600.0,
    86_400.0,
    259_200.0,
)

# Passages per search, one bucket per possible count up to the top-k ceiling.
RETRIEVED_PASSAGE_BUCKETS: Final[tuple[float, ...]] = (0.0, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)

REDIS_HISTOGRAMS: Final[dict[str, tuple[str, tuple[str, ...], tuple[float, ...]]]] = {
    "wasla_provider_request_duration_seconds": (
        "How long a call to an external provider took, whether or not it succeeded.",
        ("provider", "operation"),
        PROVIDER_LATENCY_BUCKETS,
    ),
    "wasla_oldest_pending_payment_age_seconds": (
        "Age of the oldest collection attempt whose provider outcome is unknown.",
        (),
        PENDING_PAYMENT_AGE_BUCKETS,
    ),
    # The hosted-checkout half of the same question (BILL-09, BILL-14): a
    # customer who paid on a Paymob page whose callback never arrived. Observed
    # by each reconciliation pass; an hour-old value means callbacks are not
    # reaching the deployment.
    "wasla_oldest_pending_hosted_payment_age_seconds": (
        "Age of the oldest hosted-checkout payment still pending past the grace period.",
        (),
        PENDING_PAYMENT_AGE_BUCKETS,
    ),
    # A whole knowledge search - query embedding and vector query - whatever it
    # ended in. No labels: the outcome split is the counter above.
    "wasla_rag_retrieval_duration_seconds": (
        "How long a knowledge search took, embedding included.",
        (),
        PROVIDER_LATENCY_BUCKETS,
    ),
    # How many passages a search handed the model. Bounded by the top-k ceiling,
    # so the buckets are the whole domain.
    "wasla_rag_retrieved_passages": (
        "Passages a knowledge search returned after the relevance threshold.",
        (),
        RETRIEVED_PASSAGE_BUCKETS,
    ),
    # How long one tool call took, from the executor taking it up to its
    # terminal state (TOOL-11). Labelled by tool alone: the outcome split is the
    # counter above, and crossing the two would multiply the series for a
    # question nobody asks. Provider-latency buckets, because the one tool that
    # calls anybody else dominates the distribution.
    "wasla_agent_tool_execution_duration_seconds": (
        "How long an agent tool call took, refusals included.",
        ("tool",),
        PROVIDER_LATENCY_BUCKETS,
    ),
}


def _field(labels: Mapping[str, str]) -> str:
    """The hash field one label combination occupies.

    Sorted, so the same combination always lands on the same field however the
    call site spelled it.
    """
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def _parse_field(field: str) -> dict[str, str] | None:
    """Labels back out of a hash field, or None if the field does not parse.

    **An empty field is an answer rather than a failure.** A metric with no
    labels occupies exactly one hash field, and `_field({})` spells that field
    as the empty string - so reading it as unparseable meant every sample of an
    unlabelled cross-process metric was written to Redis and then dropped on
    the way out. `wasla_oldest_pending_payment_age_seconds` is the only one, and
    it is the metric ADR-088 nominates as the alerting signal for an attempt
    nobody can resolve: the exposition carried its HELP and TYPE and never a
    single bucket, so the alert written against it could not fire.

    None is still returned for a field that is genuinely malformed - a part
    with no `=`, or an empty key - which is what keeps a field written by an
    older release from being read as a label combination it is not.
    """
    if not field:
        return {}
    labels: dict[str, str] = {}
    for part in field.split(","):
        key, separator, value = part.partition("=")
        if not separator or not key:
            return None
        labels[key] = value
    return labels


# Where cross-process counters are written, set once per process at start-up.
#
# Module-level, like `REGISTRY` above and for the same reason: a counter
# belongs to the code that emits it, not to a request. The alternative was
# threading a Redis handle through `WhatsAppClient`, `ResponsesClient` and
# every Paymob provider - none of which has one, all of which would then take
# an argument they use for nothing but counting. That is instrumentation
# owning the code it observes, which is the thing this whole module is
# supposed to avoid.
#
# Unset is the ordinary state in a test and in any process that has not opted
# in, and it makes every `record_*` call below a no-op rather than an error.
_sink: Redis | None = None


def set_counter_sink(redis: Redis | None) -> None:
    """Point cross-process counters at this process's Redis client."""
    global _sink
    _sink = redis


def counter_sink() -> Redis | None:
    return _sink


async def _increment(metric: str, labels: Mapping[str, str]) -> None:
    await _increment_by(metric, labels, 1)


async def _increment_by(metric: str, labels: Mapping[str, str], amount: int) -> None:
    redis = _sink
    if redis is None:
        return
    try:
        await cast("Any", redis.hincrby(f"{COUNTER_PREFIX}:{metric}", _field(labels), amount))
    except Exception:
        # A counter that cannot be written is a sample lost, and losing a
        # sample is not a reason to fail the work being sampled.
        logger.warning(
            "metrics.record_failed",
            extra={"event": "metrics.record_failed", "metric": metric},
        )


async def _observe(
    metric: str,
    labels: Mapping[str, str],
    value: float,
    bounds: tuple[float, ...],
) -> None:
    """Add one observation to a cross-process distribution.

    **Two commands, not eleven.** A Prometheus histogram is cumulative, so the
    obvious implementation increments every bucket at or above the observed
    value. Written to Redis that would be one command per bucket on a path that
    runs beside every provider call. Instead the bucket the value *lands in* is
    incremented, and `render_histogram_lines` accumulates at scrape time -
    which produces exactly the same exposition, and moves the cost from the
    thousands of calls to the handful of scrapes.

    Pipelined without a transaction: the two fields are independent, and a
    crash between them loses the tail of one sample rather than corrupting
    anything. Not worth a `MULTI` on the request path.
    """
    redis = _sink
    if redis is None:
        return
    bound = bucket_for(value, bounds)
    suffix = "le=+Inf" if bound is None else f"le={bound!r}"
    prefix = _field(labels)
    try:
        pipeline = redis.pipeline(transaction=False)
        pipeline.hincrby(f"{HISTOGRAM_PREFIX}:{metric}", f"{prefix}{BUCKET_SEPARATOR}{suffix}", 1)
        pipeline.hincrbyfloat(
            f"{HISTOGRAM_PREFIX}:{metric}",
            f"{prefix}{BUCKET_SEPARATOR}{SUM_FIELD}",
            value,
        )
        await cast("Any", pipeline.execute())
    except Exception:
        logger.warning(
            "metrics.record_failed",
            extra={"event": "metrics.record_failed", "metric": metric},
        )


async def record_job_outcome(
    *,
    queue: str,
    outcome: JobOutcome,
    category: str | None = None,
) -> None:
    """One finished attempt at a queued job.

    `category` is a `FailureCategory` member for anything that did not
    succeed. It is passed as a string rather than the enum so this module
    stays free of an import from `app.workers`, which imports the application
    the way a leaf should not.
    """
    await _increment("wasla_jobs_total", {"queue": queue, "outcome": str(outcome)})
    if category is not None:
        await _increment("wasla_job_failures_total", {"queue": queue, "category": category})


async def record_provider_call(
    *,
    provider: Provider,
    operation: str,
    outcome: CallOutcome,
    duration_seconds: float | None = None,
) -> None:
    """One call to somebody else's API.

    `operation` is a short constant chosen at the call site — `send_message`,
    `respond`, `create_intention` — and never anything derived from the
    request. A handful per provider is the intended domain.

    `duration_seconds` is how long the whole operation took, including any
    retries the client made inside it, because that is the number the work
    waited on. It is optional for the one call that is not an outbound request
    at all: an inbound WhatsApp delivery is counted here so an operator can see
    Meta has stopped calling, and it has no duration this process could
    measure.
    """
    await _increment(
        "wasla_provider_requests_total",
        {"provider": str(provider), "operation": operation, "outcome": str(outcome)},
    )
    if duration_seconds is not None:
        metric = "wasla_provider_request_duration_seconds"
        await _observe(
            metric,
            {"provider": str(provider), "operation": operation},
            duration_seconds,
            REDIS_HISTOGRAMS[metric][2],
        )


async def record_provider_attempt(
    *,
    provider: Provider,
    operation: str,
    outcome: CallOutcome,
) -> None:
    """One HTTP attempt inside a provider call, whether or not it is retried.

    Recorded beside `record_provider_call` rather than instead of it: the call's
    final outcome is what a turn experienced, and the attempts are what the
    provider did. A rate limit the retry absorbed shows up only here.
    """
    await _increment(
        "wasla_provider_attempts_total",
        {"provider": str(provider), "operation": operation, "outcome": str(outcome)},
    )


async def record_ingestion_outcome(outcome: str) -> None:
    """One ingestion job, by how it ended (an `IndexingOutcome` value)."""
    await _increment("wasla_rag_ingestion_outcomes_total", {"outcome": outcome})


async def record_retrieval(*, outcome: str, duration_seconds: float, passages: int) -> None:
    """One knowledge search: `found`, `empty` or `failed`, how long, how many kept."""
    await _increment("wasla_rag_retrievals_total", {"outcome": outcome})
    await _observe(
        "wasla_rag_retrieval_duration_seconds", {}, duration_seconds, PROVIDER_LATENCY_BUCKETS
    )
    await _observe("wasla_rag_retrieved_passages", {}, float(passages), RETRIEVED_PASSAGE_BUCKETS)


async def record_agent_turn_outcome(outcome: str) -> None:
    """One finished agent turn, by how it ended.

    `outcome` is a `TurnOutcome` value, passed as a string so this leaf module
    does not import the models, the same arrangement `record_job_outcome` has
    with `FailureCategory`.
    """
    await _increment("wasla_agent_turn_outcomes_total", {"outcome": outcome})


#: What a tool call is counted as. Six values, closed, and deliberately coarser
#: than `ToolExecutionReason`: an operator graphs "denied" and drills into the
#: reason in the table, rather than carrying eighteen reason labels in Redis.
TOOL_OUTCOMES: Final[tuple[str, ...]] = (
    "succeeded",
    "rejected",
    "denied",
    "failed",
    "duplicate",
    "ambiguous",
)

#: The label a tool this deployment does not implement is counted under. A name
#: the model invented must not become a label of its own - the conversation
#: contains text a stranger wrote, and that is the shape of a cardinality leak.
UNKNOWN_TOOL: Final = "unknown"


async def record_tool_execution(
    *,
    tool: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """One agent tool call: which tool, how it ended, how long it took.

    `tool` must already have been checked against the registry by the caller -
    see `UNKNOWN_TOOL`. `outcome` is one of `TOOL_OUTCOMES`, passed as a string
    so this leaf module does not import the models, the same arrangement
    `record_agent_turn_outcome` has with `TurnOutcome`.
    """
    await _increment("wasla_agent_tool_executions_total", {"tool": tool, "outcome": outcome})
    await _observe(
        "wasla_agent_tool_execution_duration_seconds",
        {"tool": tool},
        duration_seconds,
        PROVIDER_LATENCY_BUCKETS,
    )


@dataclass(slots=True)
class ProviderCall:
    """One call to somebody else, timed from the moment it was started.

    The alternative was a `duration_seconds=` argument at every exit of every
    provider client, computed from a `perf_counter()` the call site had to
    remember to take. There are four clients and sixteen exits between them,
    each recording a different outcome, and "the one that forgot to time
    itself" is exactly the kind of omission that shows up as a metric quietly
    missing a provider rather than as a failure.

    So the clock starts when the object is made, which is the first statement
    of the operation, and every exit says only how it ended.

    `record` swallows, because everything in this module does: a call to
    somebody else's API must not fail because the observation of it did.
    """

    provider: Provider
    operation: str
    _started: float = field(default_factory=perf_counter, init=False)
    # Wall-clock beside the monotonic one, because a span is placed on a
    # timeline shared with other processes and `perf_counter` has no epoch.
    # The duration still comes from `perf_counter`, which is the clock that
    # cannot go backwards when somebody adjusts the system time mid-call.
    _started_ns: int = field(default_factory=time_ns, init=False)

    async def record(self, outcome: CallOutcome) -> None:
        """Close the call: one counter, one latency sample, one span.

        The span is written *after the fact*, with the start time this object
        recorded and the end time now. That is deliberate rather than a
        shortcut around a context manager: every provider client here is a
        retry loop with a dozen exits, and a span opened at the top would have
        to be closed on all of them. Created and ended in one statement, there
        is no path on which one is left open.

        Nothing runs inside a provider call, so nothing is lost by the span not
        being the current one while the call is in flight.
        """
        await record_provider_call(
            provider=self.provider,
            operation=self.operation,
            outcome=outcome,
            duration_seconds=perf_counter() - self._started,
        )
        record_span(
            f"provider.{self.provider}.{self.operation}",
            kind=SpanKind.CLIENT,
            started_at_ns=self._started_ns,
            attributes={
                PROVIDER: str(self.provider),
                PROVIDER_OPERATION: self.operation,
                PROVIDER_OUTCOME: str(outcome),
            },
            # The outcome enum, never the provider's own reason: their error
            # catalogue is unbounded, changes without notice, and its text can
            # echo the request.
            error=None if outcome is CallOutcome.SUCCESS else str(outcome),
        )

    async def attempt(self, outcome: CallOutcome) -> None:
        """Count one attempt inside this call (AI-09); `record` still closes it."""
        await record_provider_attempt(
            provider=self.provider,
            operation=self.operation,
            outcome=outcome,
        )


async def record_retention_pass(*, purged: int, failed: int, pending: int) -> None:
    """One retention sweep's result.

    `pending` is a level rather than a count of events, and it is written to a
    counter anyway - deliberately. The alternative was a fourth mechanism for
    cross-process gauges, and what an operator actually asks of this number is
    "is it going up?", which a monotonically-increasing sum answers as well as a
    gauge does while costing nothing new. Documented here because a counter
    named like a level is exactly the sort of thing somebody reads wrongly.
    """
    if purged:
        await _increment_by("wasla_media_retention_total", {"outcome": "purged"}, purged)
    if failed:
        await _increment_by("wasla_media_retention_total", {"outcome": "failed"}, failed)
    if pending:
        await _increment_by("wasla_media_retention_total", {"outcome": "pending"}, pending)


async def record_media_outcome(outcome: str) -> None:
    """One attachment reached its terminal state (MEDIA-15)."""
    label = outcome if outcome in MEDIA_OUTCOMES else "unknown"
    await _increment("wasla_media_outcomes_total", {"outcome": label})


async def record_media_recovery(
    *, requeued: int, abandoned: int, release_failed: int, release_recovered: int = 0
) -> None:
    """One recovery sweep's decisions (MEDIA-03), and its owed-turn republishing."""
    if requeued:
        await _increment_by("wasla_media_recovery_total", {"outcome": "requeued"}, requeued)
    if abandoned:
        await _increment_by("wasla_media_recovery_total", {"outcome": "abandoned"}, abandoned)
    if release_failed:
        await _increment_by(
            "wasla_media_recovery_total", {"outcome": "release_failed"}, release_failed
        )
    if release_recovered:
        await _increment_by(
            "wasla_media_recovery_total", {"outcome": "release_recovered"}, release_recovered
        )


async def record_media_purge_objects(*, deleted: int, failed: int) -> None:
    """One purge drain's object deletes (MEDIA-07)."""
    if deleted:
        await _increment_by("wasla_media_purge_objects_total", {"outcome": "deleted"}, deleted)
    if failed:
        await _increment_by("wasla_media_purge_objects_total", {"outcome": "failed"}, failed)


async def record_upload_reconciliation(
    *,
    finalized: int,
    missing: int,
    mismatched: int,
    unreachable: int,
    pending: int,
    quarantined: int,
) -> None:
    """One reconciliation pass's result (ADR-087).

    `pending` and `mismatched` are levels written to a counter, for the reason
    `record_retention_pass` sets out: what an operator asks of either is "is it
    going up?", and a monotonic sum answers that as well as a gauge would
    without a fourth mechanism for cross-process gauges.

    The two are not redundant with the per-pass verdicts beside them.
    `mismatched` counts what *this* pass discovered; `quarantined` counts
    everything still in that state, which is what stays above zero until
    somebody looks at it.

    Six plain integers rather than the reconciler's own result object, so this
    module keeps importing nothing from `app.services` - `core` is underneath
    the services, and a metric writer that had to know a service's types would
    invert that.
    """
    counts = {
        "finalized": finalized,
        "missing": missing,
        "mismatched": mismatched,
        "unreachable": unreachable,
        "pending": pending,
        "quarantined": quarantined,
    }
    for label, amount in counts.items():
        if amount:
            await _increment_by(
                "wasla_media_upload_reconciliation_total", {"outcome": label}, amount
            )


async def record_payment_reconciliation(
    *,
    settled: int,
    failed: int,
    abandoned: int,
    still_pending: int,
    not_found: int,
    unreachable: int,
    pending: int,
    oldest_pending_seconds: float,
) -> None:
    """One reconciliation pass's result (ADR-088).

    One label with seven fixed values, and no identifier among them: no
    workspace, no invoice, no payment, no provider reference, no amount. A
    payment's value is exactly what a metric label domain must never be keyed
    on, and a reference is unbounded.

    `pending` is a level written to a counter, for the reason
    `record_upload_reconciliation` sets out: what an operator asks of it is "is
    it going up", and a monotonic sum answers that as well as a gauge would
    without a fourth mechanism for cross-process gauges.

    `oldest_pending_seconds` is the one figure that is not a count, and it is
    the one an alert should fire on. A backlog of one is a callback in flight;
    a backlog of one that is a day old is an invoice nobody can collect and
    possibly a customer who has already paid. It is written as its own metric
    rather than squeezed into the counter, because adding ages together is
    meaningless.
    """
    counts = {
        "settled": settled,
        "failed": failed,
        "abandoned": abandoned,
        "still_pending": still_pending,
        "not_found": not_found,
        "unreachable": unreachable,
        "pending": pending,
    }
    for label, amount in counts.items():
        if amount:
            await _increment_by("wasla_payment_reconciliation_total", {"outcome": label}, amount)
    if oldest_pending_seconds > 0:
        metric = "wasla_oldest_pending_payment_age_seconds"
        await _observe(metric, {}, oldest_pending_seconds, REDIS_HISTOGRAMS[metric][2])


# The closed label domains of the billing outcome counters (BILL-14). A value
# outside its metric's set is counted as `other`, so no caller - and nothing a
# provider sends - can widen a label domain.
BILLING_OUTCOMES: Final[dict[str, frozenset[str]]] = {
    "wasla_billing_checkouts_total": frozenset({"created", "settled", "failed"}),
    "wasla_billing_payments_total": frozenset(
        {"succeeded", "declined", "failed", "refused", "mismatched", "duplicate"}
    ),
    "wasla_billing_renewals_total": frozenset(
        {
            "invoiced",
            "charge_requested",
            "not_sent",
            "provider_refused",
            "outcome_unknown",
            "permanent_failure",
            "skipped",
        }
    ),
    "wasla_billing_callbacks_total": frozenset(
        {
            "applied",
            "duplicate",
            "unmatched",
            "mismatched",
            "no_change",
            "refused",
            "declined",
            "rejected_signature",
        }
    ),
    "wasla_billing_refunds_total": frozenset(
        {"requested", "refused", "confirmed", "review_requested"}
    ),
    "wasla_billing_hosted_reconciliation_total": frozenset(
        {"settled", "declined", "still_pending", "not_found", "unreachable", "expired"}
    ),
    "wasla_billing_incidents_total": frozenset(
        {
            "duplicate_payment",
            "refused_settlement",
            "mismatched_callback",
            "unknown_callback",
            "permanent_provider_error",
            "recovered_by_reconciliation",
            "refund_requested",
        }
    ),
}


def _closed(metric: str, value: str) -> str:
    """`value` if it is in the metric's closed domain, else `other`."""
    return value if value in BILLING_OUTCOMES[metric] else "other"


async def _tolerate(recording: Awaitable[None]) -> None:
    """Await a recording; a metric sink that fails loses the sample, not the caller."""
    try:
        await recording
    except Exception:
        logger.warning("metrics.record_failed", extra={"event": "metrics.record_failed"})


async def record_billing_checkout(outcome: str) -> None:
    """A hosted checkout was created, settled or failed (BILL-14)."""
    metric = "wasla_billing_checkouts_total"
    await _tolerate(_increment(metric, {"outcome": _closed(metric, outcome)}))


async def record_billing_payment(outcome: str) -> None:
    """What settlement decided about one provider payment (BILL-14)."""
    metric = "wasla_billing_payments_total"
    await _tolerate(_increment(metric, {"outcome": _closed(metric, outcome)}))


async def record_billing_renewal(outcome: str) -> None:
    """One step of automatic renewal (BILL-14)."""
    metric = "wasla_billing_renewals_total"
    await _tolerate(_increment(metric, {"outcome": _closed(metric, outcome)}))


async def record_billing_callback(outcome: str) -> None:
    """What happened to one provider callback (BILL-14)."""
    metric = "wasla_billing_callbacks_total"
    await _tolerate(_increment(metric, {"outcome": _closed(metric, outcome)}))


async def record_billing_refund(outcome: str) -> None:
    """A refund was requested, refused, confirmed or asked for review (BILL-13)."""
    metric = "wasla_billing_refunds_total"
    await _tolerate(_increment(metric, {"outcome": _closed(metric, outcome)}))


async def record_billing_incident(kind: str) -> None:
    """A durable billing incident was raised (BILL-15)."""
    metric = "wasla_billing_incidents_total"
    await _tolerate(_increment(metric, {"kind": _closed(metric, kind)}))


async def record_hosted_reconciliation(
    outcomes: dict[str, int],
    *,
    oldest_pending_seconds: float,
) -> None:
    """One hosted-checkout reconciliation pass (BILL-09)."""
    metric = "wasla_billing_hosted_reconciliation_total"
    for label, amount in outcomes.items():
        if amount:
            await _tolerate(_increment_by(metric, {"outcome": _closed(metric, label)}, amount))
    if oldest_pending_seconds > 0:
        name = "wasla_oldest_pending_hosted_payment_age_seconds"
        await _tolerate(_observe(name, {}, oldest_pending_seconds, REDIS_HISTOGRAMS[name][2]))


async def read_redis_counters(redis: Redis) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Every cross-process counter, ready to render.

    Fields the guard would refuse, or that no longer parse, are dropped rather
    than raised on: this reads whatever is in Redis, including whatever an
    older release wrote, and one unreadable field must not cost the scrape
    every other sample.
    """
    collected: dict[str, list[tuple[dict[str, str], float]]] = {}
    for metric, (_, expected) in REDIS_COUNTERS.items():
        try:
            raw = await cast("Any", redis.hgetall(f"{COUNTER_PREFIX}:{metric}"))
        except Exception:
            logger.warning(
                "metrics.read_failed",
                extra={"event": "metrics.read_failed", "metric": metric},
            )
            continue
        samples: list[tuple[dict[str, str], float]] = []
        for raw_field, value in (raw or {}).items():
            labels = _parse_field(raw_field)
            if labels is None or set(labels) != set(expected):
                continue
            try:
                samples.append((labels, float(value)))
            except (TypeError, ValueError):
                continue
        collected[metric] = samples
    return collected


async def read_redis_histograms(redis: Redis) -> dict[str, list[HistogramSample]]:
    """Every cross-process distribution, ready to render.

    Reads whatever is in Redis, including whatever an older release wrote, and
    drops what it cannot make sense of rather than raising: a field whose
    labels no longer match the declaration, a bound this release does not
    declare, a value that will not parse. One unreadable field must not cost
    the scrape every other sample.

    A label combination with observations but no `sum` field still renders -
    the sum reads as zero, the buckets are right, and a scrape mid-write is the
    only way to reach that state. The reverse (a sum with no buckets) renders
    as an empty distribution, which is what it is.
    """
    collected: dict[str, list[HistogramSample]] = {}
    for metric, (_, expected, bounds) in REDIS_HISTOGRAMS.items():
        try:
            raw = await cast("Any", redis.hgetall(f"{HISTOGRAM_PREFIX}:{metric}"))
        except Exception:
            logger.warning(
                "metrics.read_failed",
                extra={"event": "metrics.read_failed", "metric": metric},
            )
            continue
        collected[metric] = _histogram_samples(raw or {}, expected=expected, bounds=bounds)
    return collected


def _histogram_samples(
    raw: Mapping[str, str],
    *,
    expected: tuple[str, ...],
    bounds: tuple[float, ...],
) -> list[HistogramSample]:
    """Group `<labels>|<bucket>` fields back into one sample per combination."""
    buckets: dict[tuple[tuple[str, str], ...], dict[float, float]] = {}
    overflow: dict[tuple[tuple[str, str], ...], float] = {}
    totals: dict[tuple[tuple[str, str], ...], float] = {}
    seen: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}

    for raw_field, raw_value in raw.items():
        prefix, separator, suffix = raw_field.rpartition(BUCKET_SEPARATOR)
        if not separator:
            continue
        labels = _parse_field(prefix)
        if labels is None or set(labels) != set(expected):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        key = tuple(sorted(labels.items()))
        seen[key] = labels
        if suffix == SUM_FIELD:
            totals[key] = value
        elif suffix == "le=+Inf":
            overflow[key] = value
        elif suffix.startswith("le="):
            try:
                bound = float(suffix[3:])
            except ValueError:
                continue
            if bound not in bounds:
                # A bound this release no longer declares. Dropped rather than
                # folded into a neighbour: quietly moving observations between
                # buckets would make a quantile computed across a bucket change
                # look like an answer.
                continue
            buckets.setdefault(key, {})[bound] = value

    return [
        HistogramSample(
            labels=labels,
            buckets=buckets.get(key, {}),
            overflow=overflow.get(key, 0.0),
            total=totals.get(key, 0.0),
        )
        for key, labels in sorted(seen.items())
    ]


__all__ = [
    "BUCKET_SEPARATOR",
    "COUNTER_PREFIX",
    "DEPENDENCY_FAILURES",
    "DEPENDENCY_UP",
    "HISTOGRAM_PREFIX",
    "HTTP_IN_FLIGHT",
    "HTTP_LATENCY",
    "HTTP_REQUESTS",
    "REDIS_COUNTERS",
    "REDIS_HISTOGRAMS",
    "TOOL_OUTCOMES",
    "UNHANDLED_ERRORS",
    "UNKNOWN_TOOL",
    "CallOutcome",
    "JobOutcome",
    "Provider",
    "ProviderCall",
    "counter_sink",
    "observe_auth_event",
    "observe_dependency",
    "observe_http",
    "observe_unhandled_error",
    "read_redis_counters",
    "read_redis_histograms",
    "record_job_outcome",
    "record_payment_reconciliation",
    "record_provider_call",
    "record_retention_pass",
    "record_tool_execution",
    "record_upload_reconciliation",
    "set_counter_sink",
]
