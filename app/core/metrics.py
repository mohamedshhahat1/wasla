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


__all__ = [
    "DEFAULT_LATENCY_BUCKETS",
    "MAX_LABEL_VALUE_LENGTH",
    "REGISTRY",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricLabelError",
    "MetricsRegistry",
    "render_gauge_lines",
]
