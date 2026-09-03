"""Counters, gauges and histograms, and the text a scraper reads them from.

Written here rather than pulled in, for the reason the OpenAI integration is
written here rather than pulled in (ADR-013): the surface actually used is
small, well specified and stable, and a dependency would bring a registry
model, a multiprocess mode and a collector protocol that this application has
no use for. What it would *not* bring is the one property this module exists
to enforce.

**A label value must be bounded, and that is checked rather than trusted.**
Metrics are the one place where a careless identifier does lasting damage: a
`tenant_id` label does not leak a workspace's data, it multiplies every series
by the number of workspaces until the scraper falls over, and it does so
silently, weeks after the line was written. `_reject_unbounded` therefore
refuses anything that looks like an identifier - a UUID, an address, a long
string, a phone-shaped run of digits - at the moment a sample is recorded. It
raises, so a test can prove the guard works; every call site in the
application goes through `app.core.telemetry`, which swallows, so a metric can
never take a request down with it.

The exposition format is Prometheus text 0.0.4, which is what almost anything
scrapes: OpenTelemetry collectors, Grafana Agent, VictoriaMetrics and
Prometheus itself. Nothing here commits the deployment to a particular one.
"""

from __future__ import annotations

import math
import re
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# Prometheus' own rule for a metric or label name.
_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# The longest a label value may be. Sized to the application's own longest
# route template with room to grow - `/api/v1/conversations/{conversation_id}/
# messages/template` is 57 characters - because a route template is the one
# legitimately long label here. Everything else is an enum member or an HTTP
# method. The limit's job is to stop *prose* becoming a label: an error
# message, a provider's reason, a model's answer. The shape checks below are
# what stop an identifier, and they do not depend on length.
MAX_LABEL_VALUE_LENGTH: Final = 96

# A run of digits this long is a phone number, an account reference or an
# epoch. None of them are a category.
_DIGIT_RUN = re.compile(r"^\d{7,}$")

# Seconds. Chosen for an API in front of a database and a provider: the first
# five bucket a healthy request, and the last three are where an incident
# lives. Ten buckets per series is a deliberate ceiling - a histogram is the
# most expensive thing here, and doubling the buckets doubles the cost of
# every route.
DEFAULT_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


# Seconds, for a call that crosses the internet to somebody else's API.
# Deliberately not `DEFAULT_LATENCY_BUCKETS`: those start at 5 ms because an
# in-process handler can finish in one, and no provider call ever will, so the
# first three would be permanently zero - ten buckets is the ceiling this
# module sets and three of them would carry no information.
#
# The range is chosen from the timeouts actually configured: JWKS 5 s,
# WhatsApp and Google token 10 s, Resend 15 s, Paymob 20 s, OpenAI 60 s. So the
# top bound is 60 and `+Inf` collects an inference that ran to its timeout plus
# the retries above it. The middle - 0.25 to 2.5 - is where a healthy call to
# any of them lands, and 5 to 30 is where an incident does.
PROVIDER_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


class MetricLabelError(ValueError):
    """A label name or value that must not become a time series.

    Raised, not logged. The guard is only worth having if something fails when
    it trips, and the place that must not fail - the request path - reaches
    metrics through `app.core.telemetry`, which contains this.
    """


def _check_name(name: str, *, what: str) -> None:
    if not _NAME.match(name):
        raise MetricLabelError(f"{what} {name!r} is not a valid Prometheus name")


def _reject_unbounded(label: str, value: str) -> None:
    """Refuse a label value whose domain is not a small fixed set.

    Deliberately mechanical rather than clever. It cannot know that `starter`
    is a plan code and `f47ac10b-…` is a workspace, so it recognises the
    *shapes* identifiers come in and refuses those. Every legitimate label
    value in this application is a short enum member, an HTTP method, a status
    class or a route template, and none of them look like any of these.
    """
    if not value:
        raise MetricLabelError(f"label {label!r} was given an empty value")
    if len(value) > MAX_LABEL_VALUE_LENGTH:
        raise MetricLabelError(
            f"label {label!r} value is {len(value)} characters; "
            f"a label value must be a bounded category, not prose or an identifier"
        )
    try:
        uuid.UUID(value)
    except ValueError:
        pass
    else:
        raise MetricLabelError(
            f"label {label!r} was given a UUID. Identifiers - workspace, user, "
            "conversation, invoice, payment - must never become label values."
        )
    if "@" in value:
        raise MetricLabelError(f"label {label!r} looks like an email address")
    if _DIGIT_RUN.match(value):
        raise MetricLabelError(f"label {label!r} looks like a phone number or a reference")


def _escape_help(text: str) -> str:
    return text.replace("\\", r"\\").replace("\n", r"\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _render_value(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _series(name: str, labels: Mapping[str, str], value: float) -> str:
    if not labels:
        return f"{name} {_render_value(value)}"
    rendered = ",".join(f'{key}="{_escape_label(labels[key])}"' for key in sorted(labels))
    return f"{name}{{{rendered}}} {_render_value(value)}"


class _Metric:
    """Shared bookkeeping: the name, the help text, the declared label names.

    Label *names* are fixed at declaration. A sample carrying a name the
    metric did not declare is refused, which is what stops a well-meaning
    `extra={"tenant_id": ...}` habit from reaching a series.
    """

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        _check_name(name, what="metric name")
        for label in labels:
            _check_name(label, what="label name")
        self._name = name
        self._help = help_text
        self._labels = tuple(labels)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def _key(self, labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
        given = labels or {}
        if set(given) != set(self._labels):
            raise MetricLabelError(
                f"metric {self._name!r} declares labels {sorted(self._labels)}; "
                f"got {sorted(given)}"
            )
        for label, value in given.items():
            _reject_unbounded(label, value)
        return tuple(sorted(given.items()))

    def _header(self) -> list[str]:
        return [
            f"# HELP {self._name} {_escape_help(self._help)}",
            f"# TYPE {self._name} {self.kind}",
        ]

    def render(self) -> list[str]:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError


class Counter(_Metric):
    """A number that only goes up.

    A scraper handles a reset - a process restart, a flushed Redis - by
    noticing the value fell, so nothing here has to preserve totals across a
    restart.
    """

    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def increment(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("a counter cannot decrease")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            snapshot = dict(self._values)
        lines = self._header()
        for key, value in sorted(snapshot.items()):
            lines.append(_series(self._name, dict(key), value))
        return lines


class Gauge(_Metric):
    """A number that goes both ways."""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def add(self, amount: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            snapshot = dict(self._values)
        lines = self._header()
        for key, value in sorted(snapshot.items()):
            lines.append(_series(self._name, dict(key), value))
        return lines


class Histogram(_Metric):
    """Cumulative buckets, a sum and a count, which is what a quantile needs."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labels)
        bounds = tuple(sorted(buckets))
        if not bounds:
            raise ValueError("a histogram needs at least one bucket")
        self._bounds = bounds
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._sums: dict[tuple[tuple[str, str], ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self._bounds) + 1))
            self._sums[key] = self._sums.get(key, 0.0) + value
            for index, bound in enumerate(self._bounds):
                if value <= bound:
                    counts[index] += 1
            counts[-1] += 1

    def count(self, **labels: str) -> int:
        counts = self._counts.get(self._key(labels))
        return counts[-1] if counts else 0

    def render(self) -> list[str]:
        with self._lock:
            snapshot = {
                key: (list(counts), self._sums[key]) for key, counts in self._counts.items()
            }
        lines = self._header()
        for key, (counts, total) in sorted(snapshot.items()):
            labels = dict(key)
            for index, bound in enumerate(self._bounds):
                # Already cumulative: `observe` increments every bucket the
                # value falls under, so summing them here again would count
                # each observation once per bucket above it.
                lines.append(
                    _series(
                        f"{self._name}_bucket",
                        {**labels, "le": _render_value(bound)},
                        counts[index],
                    )
                )
            lines.append(
                _series(f"{self._name}_bucket", {**labels, "le": "+Inf"}, counts[-1]),
            )
            lines.append(_series(f"{self._name}_sum", labels, total))
            lines.append(_series(f"{self._name}_count", labels, counts[-1]))
        return lines


class MetricsRegistry:
    """Every metric this process publishes, and the text a scraper reads.

    Declaration is idempotent on purpose: `counter("x", ...)` twice returns the
    same object rather than raising, so a module reimported under a test does
    not have to care whether it is the first to ask.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _declare[T: _Metric](self, metric: T) -> T:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                if type(existing) is not type(metric):
                    raise MetricLabelError(
                        f"metric {metric.name!r} is already declared as {type(existing).__name__}"
                    )
                return existing
            self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str, labels: Sequence[str] = ()) -> Counter:
        return self._declare(Counter(name, help_text, labels))

    def gauge(self, name: str, help_text: str, labels: Sequence[str] = ()) -> Gauge:
        return self._declare(Gauge(name, help_text, labels))

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> Histogram:
        return self._declare(Histogram(name, help_text, labels, buckets))

    def render(self, extra: Iterable[str] = ()) -> str:
        """The whole exposition, plus any lines a caller rendered elsewhere.

        `extra` exists for the samples that do not live in this process:
        queue depth, dead-letter depth and worker heartbeats are read from
        Redis at scrape time, because the worker deliberately serves no HTTP
        of its own (ADR-069).
        """
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for metric in sorted(metrics, key=lambda item: item.name):
            lines.extend(metric.render())
        lines.extend(extra)
        return "\n".join(lines) + "\n"

    def clear(self) -> None:
        """Forget every metric. For tests, which must not inherit each other."""
        with self._lock:
            self._metrics.clear()


# One registry per process, like every metrics library. Declared here rather
# than injected because a metric is a property of the code that emits it, not
# of a request or a session, and threading a registry through every call site
# would make instrumentation the most invasive thing in the module it observes.
REGISTRY: Final = MetricsRegistry()


def render_gauge_lines(
    name: str,
    help_text: str,
    samples: Iterable[tuple[Mapping[str, str], float]],
) -> list[str]:
    """Render gauge samples that were read from somewhere else.

    Used for the Redis-resident signals. The label guard runs here too, so a
    sample collected outside the registry is held to the same rule as one
    recorded inside it.
    """
    _check_name(name, what="metric name")
    lines = [f"# HELP {name} {_escape_help(help_text)}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        for label, label_value in labels.items():
            _check_name(label, what="label name")
            _reject_unbounded(label, label_value)
        lines.append(_series(name, labels, value))
    return lines


@dataclass(frozen=True, slots=True)
class HistogramSample:
    """One label combination's observations, as they come back from Redis.

    `buckets` is **not** cumulative — it holds the count of observations that
    landed in each bound, and `render_histogram_lines` accumulates. That is a
    property of how the sample was written rather than a rendering choice: a
    cross-process histogram increments one bucket per observation instead of
    every bucket at or above it, which turns an observation from eleven Redis
    commands into two. Prometheus wants cumulative buckets, so the accumulation
    happens once at scrape time rather than on every provider call.
    """

    labels: Mapping[str, str]
    buckets: Mapping[float, float]
    #: Observations above the largest bound, which become `le="+Inf"` alone.
    overflow: float
    #: The sum of every observed value, for `_sum`.
    total: float


def render_histogram_lines(
    name: str,
    help_text: str,
    bounds: Sequence[float],
    samples: Iterable[HistogramSample],
) -> list[str]:
    """Render histogram samples that were counted in another process.

    `render_gauge_lines`' counterpart, and it exists for the same reason: the
    process that made the provider call serves no HTTP, so the numbers reach a
    scrape through Redis and are shaped here. The label guard runs over every
    sample, so a series collected outside the registry is held to the same rule
    as one recorded inside it.

    A bound Redis holds that this deployment no longer declares is dropped
    rather than rendered. Buckets are a property of the code, not of the store,
    and a release that changes them would otherwise publish a histogram whose
    buckets are half of one shape and half of another - which is worse than a
    gap, because a quantile computed across it looks like an answer.
    """
    _check_name(name, what="metric name")
    ordered = tuple(sorted(bounds))
    lines = [f"# HELP {name} {_escape_help(help_text)}", f"# TYPE {name} histogram"]
    for sample in samples:
        for label, value in sample.labels.items():
            _check_name(label, what="label name")
            _reject_unbounded(label, value)
        running = 0.0
        for bound in ordered:
            running += sample.buckets.get(bound, 0.0)
            lines.append(
                _series(f"{name}_bucket", {**sample.labels, "le": _render_value(bound)}, running)
            )
        running += sample.overflow
        lines.append(_series(f"{name}_bucket", {**sample.labels, "le": "+Inf"}, running))
        lines.append(_series(f"{name}_sum", dict(sample.labels), sample.total))
        lines.append(_series(f"{name}_count", dict(sample.labels), running))
    return lines


def bucket_for(value: float, bounds: Sequence[float]) -> float | None:
    """The bound this observation belongs under, or `None` for the overflow.

    The half of a histogram that runs on the hot path, kept here beside the
    rendering half so the two cannot come to disagree about which side of a
    bound an observation falls on. Prometheus buckets are `le` — inclusive of
    the bound — and that is the one detail worth getting right in one place.
    """
    for bound in sorted(bounds):
        if value <= bound:
            return bound
    return None


__all__ = [
    "DEFAULT_LATENCY_BUCKETS",
    "MAX_LABEL_VALUE_LENGTH",
    "PROVIDER_LATENCY_BUCKETS",
    "REGISTRY",
    "Counter",
    "Gauge",
    "Histogram",
    "HistogramSample",
    "MetricLabelError",
    "MetricsRegistry",
    "bucket_for",
    "render_gauge_lines",
    "render_histogram_lines",
]
