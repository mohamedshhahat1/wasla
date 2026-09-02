"""What happens to a job between "it failed" and "somebody has to look at it".

The lifecycle these prove, in the order a job travels it:

    attempt 1 fails retryably   -> delayed set, attempt 2
    attempt 2 fails retryably   -> delayed set, attempt 3
    attempt 3 succeeds          -> gone, side effect happened once
    attempt N reaches the limit -> dead-letter list, exactly one record
    a permanent failure         -> dead-letter list at once, no retry loop

Every one of these used to have the same answer - dead-letter immediately -
so they are the difference this batch made rather than a restatement of it.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ExternalServiceError, ValidationError
from app.core.telemetry import COUNTER_PREFIX, set_counter_sink
from app.workers.dispatch import JobIdentity, handle_failure, record_success
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from app.workers.retry import IDEMPOTENT_RETRY, NO_RETRY, FailureCategory, RetryPolicy
from tests.fake_queue_redis import FailingRedis, FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

FAILED = "agent:jobs:failed"
DELAYED = "agent:jobs:delayed"
INFLIGHT = "agent:jobs:inflight"

POLICY = RetryPolicy(max_attempts=3, base_seconds=10.0, max_seconds=100.0, jitter_ratio=0.0)


@pytest.fixture(autouse=True)
def _no_counter_sink():
    """Counters are off unless a test opts in, and never leak into the next one."""
    set_counter_sink(None)
    yield
    set_counter_sink(None)


async def reserved(queue, redis, *, now=NOW):
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=now)
    raw = await queue.reserve(wait_seconds=1, now=now)
    return raw, JobEnvelope.decode(raw)


def identity():
    return JobIdentity(tenant_id=TENANT, job_id=CONVERSATION)


# ------------------------------------------------------- transient failure


async def test_a_transient_failure_keeps_the_job_and_counts_the_attempt():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    outcome = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ExternalServiceError("502"),
        policy=POLICY,
        now=NOW,
        jitter=0.0,
    )

    assert outcome.action == "retried"
    assert outcome.attempt == 1
    assert outcome.delay_seconds == 10.0
    assert await queue.failed_depth() == 0
    (scheduled,) = redis.zsets[DELAYED]
    assert JobEnvelope.decode(scheduled).attempt == 2


async def test_the_retry_becomes_due_at_the_computed_delay():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ExternalServiceError("502"),
        policy=POLICY,
        now=NOW,
        jitter=0.0,
    )

    (scheduled,) = redis.zsets[DELAYED]
    due_at = (NOW + timedelta(seconds=10)).timestamp()
    assert redis.zsets[DELAYED][scheduled] == pytest.approx(due_at)


async def test_attempts_climb_across_successive_failures():
    """Two failures then a success: the side effect happens exactly once."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)
    sent: list[int] = []

    now = NOW
    for _ in range(2):
        await handle_failure(
            queue,
            raw,
            envelope,
            job_type="agent",
            identity=identity(),
            error=ExternalServiceError("502"),
            policy=POLICY,
            now=now,
            jitter=0.0,
        )
        now += timedelta(minutes=5)
        raw = await queue.reserve(wait_seconds=1, now=now)
        assert raw is not None
        envelope = JobEnvelope.decode(raw)

    assert envelope.attempt == 3
    # The third attempt works.
    sent.append(1)
    await queue.release(raw)
    await record_success(job_type="agent")

    assert sent == [1]
    assert await queue.failed_depth() == 0
    assert await queue.depth() == 0
    assert await queue.delayed_depth() == 0
    assert redis.lists[INFLIGHT] == []


# ------------------------------------------------------ permanent failure


async def test_a_permanent_failure_is_not_retried_even_on_the_first_attempt():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    outcome = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ValidationError("that is not a document"),
        policy=IDEMPOTENT_RETRY,
        now=NOW,
    )

    assert outcome.action == "dead_lettered"
    assert outcome.category is FailureCategory.INVALID_REQUEST
    assert await queue.delayed_depth() == 0
    assert await queue.failed_depth() == 1


async def test_a_malformed_job_goes_straight_to_the_dead_letter_list():
    """The category is supplied rather than classified: there is no exception."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    outcome = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=JobIdentity(),
        category=FailureCategory.MALFORMED,
        policy=NO_RETRY,
        now=NOW,
    )

    assert outcome.action == "dead_lettered"
    written = json.loads(redis.lists[FAILED][0])
    assert written["category"] == "malformed"
    assert "tenant_id" not in written


# ---------------------------------------------------------- max attempts


async def test_reaching_the_attempt_limit_leaves_the_queue_for_the_dead_letter_list():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    now = NOW
    outcomes = []
    for _ in range(POLICY.max_attempts):
        outcomes.append(
            await handle_failure(
                queue,
                raw,
                envelope,
                job_type="agent",
                identity=identity(),
                error=ExternalServiceError("502"),
                policy=POLICY,
                now=now,
                jitter=0.0,
            )
        )
        now += timedelta(hours=1)
        raw = await queue.reserve(wait_seconds=1, now=now)
        if raw is None:
            break
        envelope = JobEnvelope.decode(raw)

    assert [outcome.action for outcome in outcomes] == [
        "retried",
        "retried",
        "dead_lettered",
    ]
    assert await queue.depth() == 0
    assert await queue.delayed_depth() == 0
    assert await queue.failed_depth() == 1
    assert json.loads(redis.lists[FAILED][0])["attempts"] == 3


async def test_a_job_can_never_retry_for_ever():
    """The property, rather than the arithmetic: the loop terminates."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    now = NOW
    for _ in range(50):
        await handle_failure(
            queue,
            raw,
            envelope,
            job_type="agent",
            identity=identity(),
            error=ExternalServiceError("502"),
            policy=IDEMPOTENT_RETRY,
            now=now,
            jitter=0.0,
        )
        now += timedelta(days=1)
        raw = await queue.reserve(wait_seconds=1, now=now)
        if raw is None:
            break
        envelope = JobEnvelope.decode(raw)
    else:  # pragma: no cover - only reached if the budget never runs out
        pytest.fail("the job was still being retried after fifty attempts")

    assert await queue.failed_depth() == 1
    assert json.loads(redis.lists[FAILED][0])["attempts"] == IDEMPOTENT_RETRY.max_attempts


# ------------------------------------------------------------- duplication


async def test_the_same_failure_reported_twice_writes_one_dead_letter_record():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    first = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ValidationError("no"),
        policy=NO_RETRY,
        now=NOW,
    )
    second = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ValidationError("no"),
        policy=NO_RETRY,
        now=NOW,
    )

    assert (first.action, second.action) == ("dead_lettered", "lost")
    assert await queue.failed_depth() == 1


async def test_a_reservation_another_worker_took_is_not_retried_twice():
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    first = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ExternalServiceError("502"),
        policy=POLICY,
        now=NOW,
        jitter=0.0,
    )
    second = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ExternalServiceError("502"),
        policy=POLICY,
        now=NOW,
        jitter=0.0,
    )

    assert (first.action, second.action) == ("retried", "lost")
    assert await queue.delayed_depth() == 1


# ---------------------------------------------------------------- counters


async def test_outcomes_are_counted_for_an_operator():
    redis = FakeQueueRedis()
    set_counter_sink(redis)
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ExternalServiceError("502"),
        policy=POLICY,
        now=NOW,
        jitter=0.0,
    )
    await record_success(job_type="agent")

    jobs = redis.hashes[f"{COUNTER_PREFIX}:wasla_jobs_total"]
    failures = redis.hashes[f"{COUNTER_PREFIX}:wasla_job_failures_total"]
    assert jobs["outcome=retried,queue=agent"] == 1
    assert jobs["outcome=succeeded,queue=agent"] == 1
    assert failures["category=provider_error,queue=agent"] == 1


async def test_a_counter_that_cannot_be_written_does_not_lose_the_job():
    """Instrumentation observes the work; it must never take part in it."""
    redis = FailingRedis(failing=frozenset({"hincrby"}))
    set_counter_sink(redis)
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    outcome = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ValidationError("no"),
        policy=NO_RETRY,
        now=NOW,
    )

    assert outcome.action == "dead_lettered"
    assert await queue.failed_depth() == 1


async def test_a_worker_with_no_counter_sink_still_handles_the_failure():
    """Counting is optional beside the work; the sink is unset by default here."""
    redis = FakeQueueRedis()
    queue = AgentQueue(redis)
    raw, envelope = await reserved(queue, redis)

    outcome = await handle_failure(
        queue,
        raw,
        envelope,
        job_type="agent",
        identity=identity(),
        error=ValidationError("no"),
        policy=NO_RETRY,
        now=NOW,
    )

    assert outcome.action == "dead_lettered"
