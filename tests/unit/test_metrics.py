"""The registry, the exposition format, and the guard that keeps it bounded.

The guard is the reason this module was written by hand rather than pulled in.
A metrics library will happily accept `tenant_id` as a label and give you one
time series per workspace; the failure shows up weeks later, in the scraper,
as an out-of-memory nobody can trace back to the line that caused it. So
`_reject_unbounded` refuses identifier-shaped values at the moment a sample is
recorded, and most of this file is about proving it refuses the right things
and admits the right things.
"""

import uuid

import pytest

from app.core.metrics import (
    DEFAULT_LATENCY_BUCKETS,
    MAX_LABEL_VALUE_LENGTH,
    Counter,
    Gauge,
    Histogram,
    MetricLabelError,
    MetricsRegistry,
    render_gauge_lines,
)


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


# ------------------------------------------------------------- the guard


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(str(uuid.uuid4()), id="uuid"),
        pytest.param(str(uuid.uuid4()).upper(), id="uuid-uppercase"),
        pytest.param(uuid.uuid4().hex, id="uuid-without-dashes"),
        pytest.param("customer@example.com", id="email"),
        pytest.param("201001234567", id="phone"),
        pytest.param("x" * (MAX_LABEL_VALUE_LENGTH + 1), id="prose"),
        pytest.param("", id="empty"),
    ],
)
def test_an_identifier_shaped_label_is_refused(
    registry: MetricsRegistry,
    value: str,
) -> None:
    counter = registry.counter("wasla_test_total", "help", ("subject",))

    with pytest.raises(MetricLabelError):
        counter.increment(subject=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("agent", id="queue-name"),
        pytest.param("provider_error", id="failure-category"),
        pytest.param("GET", id="http-method"),
        pytest.param("5xx", id="status-class"),
        pytest.param("/api/v1/conversations/{conversation_id}/messages/template", id="route"),
        pytest.param("starter", id="plan-code"),
    ],
)
def test_a_bounded_category_is_admitted(registry: MetricsRegistry, value: str) -> None:
    """The longest route template this application has must fit."""
    counter = registry.counter("wasla_test_total", "help", ("subject",))

    counter.increment(subject=value)

    assert counter.value(subject=value) == 1


def test_a_label_the_metric_did_not_declare_is_refused(registry: MetricsRegistry) -> None:
    """Stops a well-meaning `extra={"tenant_id": ...}` habit reaching a series."""
    counter = registry.counter("wasla_test_total", "help", ("queue",))

    with pytest.raises(MetricLabelError):
        counter.increment(queue="agent", tenant_id="acme")


def test_a_missing_label_is_refused(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help", ("queue", "outcome"))

    with pytest.raises(MetricLabelError):
        counter.increment(queue="agent")


def test_a_metric_name_that_is_not_a_prometheus_name_is_refused(registry: MetricsRegistry) -> None:
    with pytest.raises(MetricLabelError):
        registry.counter("wasla-test-total", "help")


def test_a_label_name_that_is_not_a_prometheus_name_is_refused(registry: MetricsRegistry) -> None:
    with pytest.raises(MetricLabelError):
        registry.counter("wasla_test_total", "help", ("tenant-id",))


def test_lines_rendered_outside_the_registry_face_the_same_guard() -> None:
    """Redis-derived samples are held to the rule in-process ones are."""
    with pytest.raises(MetricLabelError):
        render_gauge_lines("wasla_test", "help", [({"queue": str(uuid.uuid4())}, 1.0)])


# ------------------------------------------------------------- the metrics


def test_a_counter_counts(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help", ("queue",))

    counter.increment(queue="agent")
    counter.increment(queue="agent")
    counter.increment(queue="media")

    assert counter.value(queue="agent") == 2
    assert counter.value(queue="media") == 1


def test_a_counter_counts_each_label_combination_separately(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help", ("queue", "outcome"))

    counter.increment(queue="agent", outcome="succeeded")
    counter.increment(queue="agent", outcome="dead_lettered")

    assert counter.value(queue="agent", outcome="succeeded") == 1
    assert counter.value(queue="agent", outcome="dead_lettered") == 1


def test_a_counter_refuses_to_go_backwards(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help")

    with pytest.raises(ValueError, match="cannot decrease"):
        counter.increment(-1)


def test_a_gauge_goes_both_ways(registry: MetricsRegistry) -> None:
    gauge = registry.gauge("wasla_test", "help")

    gauge.add(3)
    gauge.add(-1)

    assert gauge.value() == 2


def test_a_histogram_buckets_cumulatively(registry: MetricsRegistry) -> None:
    histogram = registry.histogram("wasla_test_seconds", "help", buckets=(0.1, 1.0))

    histogram.observe(0.05)
    histogram.observe(0.5)
    histogram.observe(10.0)

    rendered = "\n".join(histogram.render())
    assert 'wasla_test_seconds_bucket{le="0.1"} 1' in rendered
    assert 'wasla_test_seconds_bucket{le="1"} 2' in rendered
    assert 'wasla_test_seconds_bucket{le="+Inf"} 3' in rendered
    assert "wasla_test_seconds_count 3" in rendered
    assert histogram.count() == 3


def test_the_default_buckets_are_a_bounded_set() -> None:
    """A histogram is the most expensive metric here; the ceiling is deliberate."""
    assert len(DEFAULT_LATENCY_BUCKETS) <= 12
    assert list(DEFAULT_LATENCY_BUCKETS) == sorted(DEFAULT_LATENCY_BUCKETS)


# --------------------------------------------------------- the exposition


def test_the_exposition_carries_help_and_type(registry: MetricsRegistry) -> None:
    registry.counter("wasla_test_total", "How many things happened.").increment()

    rendered = registry.render()

    assert "# HELP wasla_test_total How many things happened." in rendered
    assert "# TYPE wasla_test_total counter" in rendered
    assert "wasla_test_total 1" in rendered


def test_the_exposition_ends_with_a_newline(registry: MetricsRegistry) -> None:
    """Scrapers are entitled to a trailing newline; some refuse without one."""
    registry.counter("wasla_test_total", "help").increment()

    assert registry.render().endswith("\n")


def test_labels_are_rendered_sorted_and_quoted(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help", ("queue", "outcome"))
    counter.increment(queue="agent", outcome="succeeded")

    assert 'wasla_test_total{outcome="succeeded",queue="agent"} 1' in registry.render()


def test_a_quotation_mark_in_a_label_is_escaped(registry: MetricsRegistry) -> None:
    counter = registry.counter("wasla_test_total", "help", ("route",))
    counter.increment(route='/a"b')

    assert r'route="/a\"b"' in registry.render()


def test_extra_lines_are_appended(registry: MetricsRegistry) -> None:
    registry.counter("wasla_test_total", "help").increment()

    rendered = registry.render(extra=["# TYPE other gauge", "other 4"])

    assert rendered.endswith("# TYPE other gauge\nother 4\n")


def test_declaring_the_same_metric_twice_returns_the_same_object(registry: MetricsRegistry) -> None:
    first = registry.counter("wasla_test_total", "help", ("queue",))
    second = registry.counter("wasla_test_total", "help", ("queue",))

    first.increment(queue="agent")

    assert second is first
    assert second.value(queue="agent") == 1


def test_redeclaring_a_metric_as_a_different_type_is_refused(registry: MetricsRegistry) -> None:
    registry.counter("wasla_test_total", "help")

    with pytest.raises(MetricLabelError):
        registry.gauge("wasla_test_total", "help")


def test_a_registry_can_be_emptied(registry: MetricsRegistry) -> None:
    registry.counter("wasla_test_total", "help").increment()

    registry.clear()

    assert "wasla_test_total" not in registry.render()


def test_metric_types_are_what_they_claim(registry: MetricsRegistry) -> None:
    assert isinstance(registry.counter("a_total", "h"), Counter)
    assert isinstance(registry.gauge("b", "h"), Gauge)
    assert isinstance(registry.histogram("c_seconds", "h"), Histogram)


def test_histogram_buckets_never_decrease_and_end_at_the_count(registry: MetricsRegistry) -> None:
    """The property, not an example, because the first render got this wrong.

    `observe` increments every bucket a value falls under, so the buckets are
    already cumulative; `render` summed them a second time, which made every
    bucket above the first over-count. Stated as a monotonic-and-total
    invariant, that arithmetic cannot come back unnoticed.
    """
    histogram = registry.histogram("wasla_property_seconds", "help", buckets=(0.1, 0.5, 1.0, 5.0))
    observations = [0.01, 0.05, 0.2, 0.4, 0.6, 0.9, 2.0, 4.9, 7.0, 100.0]
    for value in observations:
        histogram.observe(value)

    counts = [int(line.rsplit(" ", 1)[1]) for line in histogram.render() if "_bucket{" in line]

    assert counts == sorted(counts), "cumulative buckets must never decrease"
    assert counts[-1] == len(observations), "the +Inf bucket is the total"
    assert counts[0] == 2  # 0.01 and 0.05
    assert counts[1] == 4  # plus 0.2 and 0.4
    assert counts[2] == 6  # plus 0.6 and 0.9
    assert counts[3] == 8  # plus 2.0 and 4.9


def test_the_observed_sum_is_the_sum_of_the_observations(registry: MetricsRegistry) -> None:
    histogram = registry.histogram("wasla_sum_seconds", "help", buckets=(1.0,))

    for value in (0.25, 0.5, 2.0):
        histogram.observe(value)

    (line,) = [entry for entry in histogram.render() if entry.startswith("wasla_sum_seconds_sum")]
    assert float(line.rsplit(" ", 1)[1]) == pytest.approx(2.75)
