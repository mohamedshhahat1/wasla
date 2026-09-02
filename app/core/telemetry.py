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
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final, cast

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.metrics import REGISTRY

logger = get_logger(__name__)

# One Redis hash per metric, field per label combination. A hash rather than a
# key per series keeps the keyspace at one entry per metric no matter how the
# labels multiply, and makes a scrape one `HGETALL` instead of a scan.
COUNTER_PREFIX: Final = "metrics:counter"


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
    """How one attempt at a queued job ended."""

    SUCCEEDED = "succeeded"
    RETRIED = "retried"
    DEAD_LETTERED = "dead_lettered"


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
}


def _field(labels: Mapping[str, str]) -> str:
    """The hash field one label combination occupies.

    Sorted, so the same combination always lands on the same field however the
    call site spelled it.
    """
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def _parse_field(field: str) -> dict[str, str] | None:
    labels: dict[str, str] = {}
    for part in field.split(","):
        key, separator, value = part.partition("=")
        if not separator or not key:
            return None
        labels[key] = value
    return labels or None


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
    redis = _sink
    if redis is None:
        return
    try:
        await cast("Any", redis.hincrby(f"{COUNTER_PREFIX}:{metric}", _field(labels), 1))
    except Exception:
        # A counter that cannot be written is a sample lost, and losing a
        # sample is not a reason to fail the work being sampled.
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
) -> None:
    """One call to somebody else's API.

    `operation` is a short constant chosen at the call site — `send_message`,
    `respond`, `create_intention` — and never anything derived from the
    request. A handful per provider is the intended domain.
    """
    await _increment(
        "wasla_provider_requests_total",
        {"provider": str(provider), "operation": operation, "outcome": str(outcome)},
    )


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
        for field, value in (raw or {}).items():
            labels = _parse_field(field)
            if labels is None or set(labels) != set(expected):
                continue
            try:
                samples.append((labels, float(value)))
            except (TypeError, ValueError):
                continue
        collected[metric] = samples
    return collected


__all__ = [
    "COUNTER_PREFIX",
    "DEPENDENCY_FAILURES",
    "DEPENDENCY_UP",
    "HTTP_IN_FLIGHT",
    "HTTP_LATENCY",
    "HTTP_REQUESTS",
    "REDIS_COUNTERS",
    "UNHANDLED_ERRORS",
    "CallOutcome",
    "JobOutcome",
    "Provider",
    "counter_sink",
    "observe_dependency",
    "observe_http",
    "observe_unhandled_error",
    "read_redis_counters",
    "record_job_outcome",
    "record_provider_call",
    "set_counter_sink",
]
