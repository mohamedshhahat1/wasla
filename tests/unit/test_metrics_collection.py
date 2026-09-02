"""What the scrape says about the queues, the workers and the providers.

These are the signals nobody could see before: how deep a queue is, how long
its oldest job has been waiting, how many jobs stopped being retried, whether
each worker loop is still beating, and how each provider's calls are ending.
The point of every one of them is that an operator learns something is wrong
without a customer telling them.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.metrics import MetricsRegistry
from app.core.telemetry import (
    CallOutcome,
    JobOutcome,
    Provider,
    record_job_outcome,
    record_provider_call,
    set_counter_sink,
)
from app.services.metrics_service import QUEUES, MetricsService
from app.workers.heartbeat import heartbeat_key
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from app.workers.retry import FailureCategory
from app.workers.runner import ALL_KINDS
from tests.fake_queue_redis import FailingRedis, FakeQueueRedis

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TENANT = "11111111-1111-1111-1111-111111111111"
CONVERSATION = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def redis():
    return FakeQueueRedis()


@pytest.fixture
def sink(redis):
    """Point the cross-process counters at this test's fake, then unhook it."""
    set_counter_sink(redis)
    yield redis
    set_counter_sink(None)


@pytest.fixture
def service(redis):
    return MetricsService(redis, registry=MetricsRegistry())


def sample(rendered: str, name: str, labels: str = "") -> float:
    prefix = f"{name}{{{labels}}} " if labels else f"{name} "
    for line in rendered.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{{{labels}}} is not in the exposition:\n{rendered}")


def absent(rendered: str, name: str) -> bool:
    return not any(
        line.startswith(name) and not line.startswith("#") for line in rendered.splitlines()
    )


# ------------------------------------------------------------ queue depth


async def test_queue_depth_is_published(service, redis):
    queue = AgentQueue(redis)
    for _ in range(3):
        await queue.enqueue_body('{"x":1}', now=NOW)

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_queue_pending_jobs", 'queue="agent"') == 3
    assert sample(rendered, "wasla_queue_pending_jobs", 'queue="media"') == 0


async def test_every_queue_appears_even_when_empty(service):
    """An absent series and a zero one mean different things to an alert."""
    rendered = await service.render(now=NOW)

    for name in QUEUES:
        assert sample(rendered, "wasla_queue_pending_jobs", f'queue="{name}"') == 0


async def test_in_flight_delayed_and_dead_letter_depths_are_separate(service, redis):
    redis.lists["agent:jobs:inflight"] = ["a", "b"]
    redis.lists["agent:jobs:failed"] = ["r"] * 4
    redis.zsets["agent:jobs:delayed"] = {"z": 1.0}

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_queue_inflight_jobs", 'queue="agent"') == 2
    assert sample(rendered, "wasla_queue_dead_letter_jobs", 'queue="agent"') == 4
    assert sample(rendered, "wasla_queue_delayed_jobs", 'queue="agent"') == 1


async def test_a_growing_dead_letter_list_is_visible(service, redis):
    """The signal that did not exist: failed work accumulating silently."""
    before = await service.render(now=NOW)
    assert sample(before, "wasla_queue_dead_letter_jobs", 'queue="agent"') == 0

    redis.lists["agent:jobs:failed"] = ["one", "two"]

    after = await service.render(now=NOW)
    assert sample(after, "wasla_queue_dead_letter_jobs", 'queue="agent"') == 2


# ------------------------------------------------------------- queue age


async def test_the_oldest_pending_job_age_is_published(service, redis):
    await AgentQueue(redis).enqueue_body('{"x":1}', now=NOW - timedelta(minutes=7))

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_queue_oldest_pending_age_seconds", 'queue="agent"') == 420


async def test_an_empty_queue_publishes_no_age_at_all(service):
    """Zero would read as "nothing has waited long", which is a different claim."""
    rendered = await service.render(now=NOW)

    assert absent(rendered, "wasla_queue_oldest_pending_age_seconds")


async def test_a_retried_job_keeps_measuring_from_its_first_enqueue(service, redis):
    """Queue age is how long the *customer* waited, not how long since the retry."""
    queue = AgentQueue(redis)
    await queue.enqueue_body('{"x":1}', now=NOW - timedelta(minutes=10))
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    await queue.schedule_retry(
        raw,
        JobEnvelope.decode(raw),
        category=FailureCategory.TIMEOUT,
        delay_seconds=1.0,
        now=NOW,
    )
    await queue.promote_due(now=NOW + timedelta(seconds=5))

    rendered = await service.render(now=NOW + timedelta(seconds=5))

    age = sample(rendered, "wasla_queue_oldest_pending_age_seconds", 'queue="agent"')
    assert age == pytest.approx(605.0)


# -------------------------------------------------------- worker heartbeat


async def test_a_beating_worker_reports_alive(service, redis):
    redis.lists[heartbeat_key("agent")] = ["1"]

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_worker_heartbeat_alive", 'kind="agent"') == 1


async def test_a_stale_worker_reports_dead_without_anything_else_failing(service, redis):
    """The key has expired, which is what a stopped loop looks like."""
    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_worker_heartbeat_alive", 'kind="agent"') == 0
    # And the rest of the exposition is still there.
    assert sample(rendered, "wasla_queue_pending_jobs", 'queue="agent"') == 0


async def test_every_worker_kind_is_reported(service):
    rendered = await service.render(now=NOW)

    for kind in ALL_KINDS:
        sample(rendered, "wasla_worker_heartbeat_alive", f'kind="{kind}"')


# ------------------------------------------------------ cross-process counters


async def test_job_outcomes_written_by_a_worker_reach_the_scrape(sink, service):
    await record_job_outcome(queue="agent", outcome=JobOutcome.SUCCEEDED)
    await record_job_outcome(
        queue="media", outcome=JobOutcome.DEAD_LETTERED, category="provider_error"
    )

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_jobs_total", 'outcome="succeeded",queue="agent"') == 1
    assert sample(rendered, "wasla_jobs_total", 'outcome="dead_lettered",queue="media"') == 1
    assert (
        sample(rendered, "wasla_job_failures_total", 'category="provider_error",queue="media"') == 1
    )


async def test_provider_calls_are_counted_by_outcome(sink, service):
    await record_provider_call(
        provider=Provider.WHATSAPP, operation="send_message", outcome=CallOutcome.SUCCESS
    )
    await record_provider_call(
        provider=Provider.WHATSAPP, operation="send_message", outcome=CallOutcome.RATE_LIMITED
    )
    await record_provider_call(
        provider=Provider.PAYMOB, operation="checkout", outcome=CallOutcome.FAILURE
    )

    rendered = await service.render(now=NOW)

    assert (
        sample(
            rendered,
            "wasla_provider_requests_total",
            'operation="send_message",outcome="success",provider="whatsapp"',
        )
        == 1
    )
    assert (
        sample(
            rendered,
            "wasla_provider_requests_total",
            'operation="send_message",outcome="rate_limited",provider="whatsapp"',
        )
        == 1
    )
    assert (
        sample(
            rendered,
            "wasla_provider_requests_total",
            'operation="checkout",outcome="failure",provider="paymob"',
        )
        == 1
    )


async def test_a_cross_process_total_is_typed_as_a_counter(sink, service):
    """A scraper needs the type to know it may compute a rate and expect resets."""
    await record_job_outcome(queue="agent", outcome=JobOutcome.SUCCEEDED)

    rendered = await service.render(now=NOW)

    assert "# TYPE wasla_jobs_total counter" in rendered


async def test_counters_with_no_sink_are_a_no_op():
    """The ordinary state of a test and of any process that has not opted in."""
    set_counter_sink(None)
    await record_job_outcome(queue="agent", outcome=JobOutcome.SUCCEEDED)


# -------------------------------------------------------------- resilience


async def test_redis_being_down_does_not_empty_the_exposition():
    """A 503 during the outage being investigated is worse than half a page."""

    class DeadRedis(FakeQueueRedis):
        async def llen(self, key):
            raise RuntimeError("Redis is gone")

    registry = MetricsRegistry()
    registry.counter("wasla_local_total", "help").increment()
    rendered = await MetricsService(DeadRedis(), registry=registry).render(now=NOW)

    assert sample(rendered, "wasla_local_total") == 1
    # And the queue gauges are simply absent, which is itself alertable.
    assert absent(rendered, "wasla_queue_pending_jobs")


async def test_an_unreadable_counter_hash_does_not_lose_the_rest():
    redis = FailingRedis(failing=frozenset({"hgetall"}))
    rendered = await MetricsService(redis, registry=MetricsRegistry()).render(now=NOW)

    # Counters could not be read, but the live queue gauges still could.
    assert sample(rendered, "wasla_queue_pending_jobs", 'queue="agent"') == 0


async def test_a_counter_field_from_an_older_release_is_skipped(service, redis):
    """The scrape reads whatever is in Redis, including what it did not write."""
    redis.hashes["metrics:counter:wasla_jobs_total"] = {
        "outcome=succeeded,queue=agent": 2,
        "queue=agent": 9,  # a shape this release does not use
        "nonsense": 4,
    }

    rendered = await service.render(now=NOW)

    assert sample(rendered, "wasla_jobs_total", 'outcome="succeeded",queue="agent"') == 2
    assert "nonsense" not in rendered


# ------------------------------------------------------------- no identifiers


async def test_no_identifier_reaches_a_label(sink, service, redis):
    """The privacy property, asserted over the whole rendered document.

    Not a spot check: the exposition is scanned for the identifiers this test
    put into the *work*, because the failure being guarded against is somebody
    adding a helpful `tenant_id=` to an instrumentation call years from now.
    """
    queue = AgentQueue(redis)
    await queue.enqueue(
        AgentJob(tenant_id=uuid.UUID(TENANT), conversation_id=uuid.UUID(CONVERSATION)),
        now=NOW,
    )
    await record_job_outcome(queue="agent", outcome=JobOutcome.DEAD_LETTERED, category="unknown")
    await record_provider_call(
        provider=Provider.WHATSAPP, operation="send_message", outcome=CallOutcome.FAILURE
    )

    rendered = await service.render(now=NOW)

    assert TENANT not in rendered
    assert CONVERSATION not in rendered
    assert "@" not in rendered
