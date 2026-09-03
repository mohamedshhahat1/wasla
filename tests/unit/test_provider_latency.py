"""How long a call to somebody else took, and where that number lives.

The counter beside this metric says how a provider call *ended*. It cannot say
how long anyone waited, which is the difference between "OpenAI is up" and
"OpenAI is up and every agent turn takes forty seconds". This file covers the
distribution that answers the second question.

Two properties are worth stating because they are easy to get wrong and
invisible when they are:

**A failed call is latency data.** A provider that times out after twenty
seconds is the most important observation this metric can hold, and a
histogram that only recorded successes would report a system getting *faster*
as it broke.

**A cross-process histogram is written non-cumulatively.** Prometheus wants
`le` buckets that each include everything below them; writing that to Redis
directly would be one command per bucket on the path beside every provider
call. So the bucket the observation lands in is incremented, and the scrape
accumulates. These tests hold the rendered exposition rather than the storage
shape, because the exposition is the contract.
"""

from __future__ import annotations

import pytest

from app.core.metrics import PROVIDER_LATENCY_BUCKETS, MetricsRegistry
from app.core.telemetry import (
    HISTOGRAM_PREFIX,
    CallOutcome,
    Provider,
    ProviderCall,
    record_provider_call,
    set_counter_sink,
)
from app.services.metrics_service import MetricsService
from tests.fake_queue_redis import FailingRedis, FakeQueueRedis

METRIC = "wasla_provider_request_duration_seconds"


@pytest.fixture
def redis() -> FakeQueueRedis:
    return FakeQueueRedis()


@pytest.fixture
def sink(redis: FakeQueueRedis) -> object:
    set_counter_sink(redis)
    yield redis
    set_counter_sink(None)


@pytest.fixture
def service(redis: FakeQueueRedis) -> MetricsService:
    return MetricsService(redis, registry=MetricsRegistry())


def sample(rendered: str, name: str, labels: str = "") -> float:
    prefix = f"{name}{{{labels}}} " if labels else f"{name} "
    for line in rendered.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{{{labels}}} is not in the exposition:\n{rendered}")


async def observe(provider: Provider, operation: str, seconds: float) -> None:
    await record_provider_call(
        provider=provider,
        operation=operation,
        outcome=CallOutcome.SUCCESS,
        duration_seconds=seconds,
    )


# ------------------------------------------------------------- the shape


async def test_a_provider_call_lands_in_the_bucket_above_it(sink, service) -> None:
    await observe(Provider.OPENAI, "respond", 0.3)

    rendered = await service.render()
    labels = 'operation="respond",provider="openai"'
    # 0.3 is above 0.25 and at or below 0.5, so it counts from 0.5 upwards.
    assert sample(rendered, f"{METRIC}_bucket", f'le="0.25",{labels}') == 0
    assert sample(rendered, f"{METRIC}_bucket", f'le="0.5",{labels}') == 1
    assert sample(rendered, f"{METRIC}_bucket", f'le="1",{labels}') == 1
    assert sample(rendered, f"{METRIC}_bucket", f'le="+Inf",{labels}') == 1


async def test_buckets_are_cumulative_in_the_exposition(sink, service) -> None:
    """The property a scraper depends on, held on the rendered document.

    Three observations at increasing latencies. Every bucket must include
    everything below it, or a quantile computed from this is meaningless.
    """
    for seconds in (0.02, 0.3, 7.0):
        await observe(Provider.WHATSAPP, "send_message", seconds)

    rendered = await service.render()
    labels = 'operation="send_message",provider="whatsapp"'
    counts = [
        sample(rendered, f"{METRIC}_bucket", f'le="{bound}",{labels}')
        for bound in ("0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10", "30", "60")
    ]
    assert counts == [1, 1, 1, 2, 2, 2, 2, 3, 3, 3]
    assert sample(rendered, f"{METRIC}_bucket", f'le="+Inf",{labels}') == 3
    assert sample(rendered, f"{METRIC}_count", labels) == 3


async def test_the_sum_is_the_total_time_waited(sink, service) -> None:
    for seconds in (0.5, 1.5, 2.0):
        await observe(Provider.PAYMOB, "checkout", seconds)

    rendered = await service.render()
    labels = 'operation="checkout",provider="paymob"'
    assert sample(rendered, f"{METRIC}_sum", labels) == pytest.approx(4.0)


async def test_a_call_slower_than_every_bucket_reaches_only_the_overflow(
    sink,
    service,
) -> None:
    """An inference that ran past its own timeout, plus the retries above it."""
    await observe(Provider.OPENAI, "respond", 95.0)

    rendered = await service.render()
    labels = 'operation="respond",provider="openai"'
    assert sample(rendered, f"{METRIC}_bucket", f'le="60",{labels}') == 0
    assert sample(rendered, f"{METRIC}_bucket", f'le="+Inf",{labels}') == 1
    assert sample(rendered, f"{METRIC}_count", labels) == 1


async def test_the_metric_is_typed_as_a_histogram(sink, service) -> None:
    await observe(Provider.EMAIL, "deliver", 0.1)

    assert f"# TYPE {METRIC} histogram" in await service.render()


async def test_the_help_text_is_published_before_anything_is_observed(service) -> None:
    """So a dashboard finds the series on a deployment that has taken no calls."""
    rendered = await service.render()

    assert f"# HELP {METRIC}" in rendered
    assert f"# TYPE {METRIC} histogram" in rendered


# ------------------------------------------------------- failures count too


async def test_a_failed_call_is_still_timed(sink, service) -> None:
    """The property that makes this metric honest under an incident."""
    await record_provider_call(
        provider=Provider.OPENAI,
        operation="respond",
        outcome=CallOutcome.UNAVAILABLE,
        duration_seconds=45.0,
    )

    rendered = await service.render()
    labels = 'operation="respond",provider="openai"'
    assert sample(rendered, f"{METRIC}_count", labels) == 1
    assert sample(rendered, f"{METRIC}_sum", labels) == pytest.approx(45.0)


async def test_the_outcome_is_not_a_label_on_the_distribution(sink, service) -> None:
    """Successes and failures share one series, deliberately.

    Splitting by outcome would quadruple the series to answer a question
    `wasla_provider_requests_total` already answers, and would make "how slow
    is this provider" a sum across four series rather than one.
    """
    await record_provider_call(
        provider=Provider.WHATSAPP,
        operation="send_message",
        outcome=CallOutcome.SUCCESS,
        duration_seconds=0.2,
    )
    await record_provider_call(
        provider=Provider.WHATSAPP,
        operation="send_message",
        outcome=CallOutcome.FAILURE,
        duration_seconds=0.2,
    )

    rendered = await service.render()
    assert "outcome=" not in _lines_for(rendered, METRIC)
    assert sample(rendered, f"{METRIC}_count", 'operation="send_message",provider="whatsapp"') == 2


def _lines_for(rendered: str, name: str) -> str:
    return "\n".join(line for line in rendered.splitlines() if line.startswith(name))


# ------------------------------------------------------------- the timer


async def test_the_timer_records_a_duration_the_call_site_never_computes(
    sink,
    service,
) -> None:
    """`ProviderCall` exists so no exit can forget to time itself."""
    call = ProviderCall(provider=Provider.PAYMOB, operation="refund")
    await call.record(CallOutcome.SUCCESS)

    rendered = await service.render()
    labels = 'operation="refund",provider="paymob"'
    assert sample(rendered, f"{METRIC}_count", labels) == 1
    # A real elapsed time rather than a placeholder: non-negative, and
    # comfortably inside the first bucket for an operation that did nothing.
    assert 0.0 <= sample(rendered, f"{METRIC}_sum", labels) < 0.05


async def test_the_timer_also_counts_the_outcome(sink, service) -> None:
    """One call site, both signals, so the two can never disagree about a call."""
    call = ProviderCall(provider=Provider.OPENAI, operation="respond")
    await call.record(CallOutcome.RATE_LIMITED)

    rendered = await service.render()
    assert (
        sample(
            rendered,
            "wasla_provider_requests_total",
            'operation="respond",outcome="rate_limited",provider="openai"',
        )
        == 1
    )
    assert sample(rendered, f"{METRIC}_count", 'operation="respond",provider="openai"') == 1


# ------------------------------------------------ what is deliberately absent


async def test_an_inbound_delivery_is_counted_but_not_timed(sink, service) -> None:
    """Meta calling us has no duration this process could honestly measure."""
    await record_provider_call(
        provider=Provider.WHATSAPP,
        operation="inbound",
        outcome=CallOutcome.SUCCESS,
    )

    rendered = await service.render()
    assert 'operation="inbound"' in rendered
    assert f'{METRIC}_count{{operation="inbound"' not in rendered


# ----------------------------------------------------------- failure modes


async def test_a_histogram_write_failure_loses_a_sample_and_nothing_else() -> None:
    """Telemetry is never a participant in the work it observes."""
    redis = FailingRedis(failing=frozenset({"hincrby", "hincrbyfloat"}))
    set_counter_sink(redis)
    try:
        await observe(Provider.OPENAI, "respond", 1.0)
    finally:
        set_counter_sink(None)

    assert redis.hashes == {}


async def test_redis_being_unreadable_does_not_empty_the_exposition() -> None:
    """A scrape during the outage an operator is investigating still answers."""
    redis = FailingRedis(failing=frozenset({"hgetall"}))
    service = MetricsService(redis, registry=MetricsRegistry())

    rendered = await service.render()

    # The distribution declares itself with no samples, and the signals that do
    # not come from a hash - the heartbeats - are still there.
    assert f"# TYPE {METRIC} histogram" in rendered
    assert "wasla_worker_heartbeat_alive" in rendered


async def test_a_bucket_this_release_no_longer_declares_is_dropped(service, redis) -> None:
    """A bound left behind by an older release must not join a new one's series.

    Folding it into a neighbour would move observations between buckets, and a
    quantile computed across that looks like an answer rather than a gap.
    """
    key = f"{HISTOGRAM_PREFIX}:{METRIC}"
    redis.hashes[key] = {
        'operation="x"|le=0.5': 1,
        "provider=openai,operation=respond|le=0.5": 3,
        "provider=openai,operation=respond|le=0.0001": 99,
        "provider=openai,operation=respond|sum": 1.5,
    }

    rendered = await service.render()
    labels = 'operation="respond",provider="openai"'
    assert sample(rendered, f"{METRIC}_bucket", f'le="0.5",{labels}') == 3
    assert sample(rendered, f"{METRIC}_count", labels) == 3
    assert "0.0001" not in rendered


async def test_a_field_that_does_not_parse_costs_only_itself(service, redis) -> None:
    key = f"{HISTOGRAM_PREFIX}:{METRIC}"
    redis.hashes[key] = {
        "nonsense": 1,
        "provider=openai|le=0.5": 7,
        "provider=openai,operation=respond|le=0.5": 2,
        "provider=openai,operation=respond|sum": 0.4,
    }

    rendered = await service.render()
    labels = 'operation="respond",provider="openai"'
    assert sample(rendered, f"{METRIC}_count", labels) == 2


async def test_no_sink_makes_an_observation_a_no_op() -> None:
    """The ordinary state in a test, and in any process that has not opted in."""
    set_counter_sink(None)

    await observe(Provider.OPENAI, "respond", 1.0)


# ---------------------------------------------------------------- cardinality


async def test_the_buckets_are_the_ones_this_release_declares(sink, service) -> None:
    """Pinned, because changing them silently splits a dashboard's history."""
    await observe(Provider.OPENAI, "respond", 0.3)

    rendered = await service.render()
    published = [
        line.split('le="', 1)[1].split('"', 1)[0]
        for line in rendered.splitlines()
        if line.startswith(f"{METRIC}_bucket")
    ]
    assert published[-1] == "+Inf"
    assert len(published) == len(PROVIDER_LATENCY_BUCKETS) + 1


async def test_no_identifier_can_become_a_latency_label(sink, service) -> None:
    """The label guard runs on this metric too, on the way in and on the way out."""
    await record_provider_call(
        provider=Provider.OPENAI,
        operation="11111111-1111-1111-1111-111111111111",
        outcome=CallOutcome.SUCCESS,
        duration_seconds=1.0,
    )

    rendered = await service.render()

    assert "11111111-1111-1111-1111-111111111111" not in rendered
