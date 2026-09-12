"""The two terminal transitions, against a real Redis running the real Lua.

`tests/unit` drives these through `FakeQueueRedis`, which emulates the scripts
rather than interpreting them - so an error in the Lua itself would pass there.
This file is where that is caught: a real `redis.asyncio` client, a real
`register_script`, and the queue's own code path.

What is being protected is small and was genuinely lossy. `dead_letter` used to
claim the in-flight entry, delete the reservation, push the record and trim, in
four round trips. The ordering was deliberate - claiming first is what makes the
record exactly-once - but a Redis failure after the claim left the job terminal
and the evidence missing (WQ-13). `schedule_retry` had the identical shape and a
worse consequence: the entry was off the in-flight list and not yet in the
delayed set, which loses the job rather than its record.

One script each. Either the whole transition happens or none of it does, and
"none of it" leaves the entry in flight where a reaper finds it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.workers.queue import (
    DEAD_LETTER_LIMIT,
    AgentQueue,
    DeadLetterRecord,
    JobEnvelope,
)
from app.workers.retry import FailureCategory

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/11"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def queue(redis: Redis) -> AgentQueue:
    return AgentQueue(
        redis,
        namespace=f"test:atomic:{uuid.uuid4().hex[:10]}",
        visibility_timeout_seconds=60,
    )


def _record(queue: AgentQueue, envelope: JobEnvelope) -> DeadLetterRecord:
    return DeadLetterRecord(
        queue=queue.namespace,
        job_type=queue.label,
        tenant_id=None,
        job_id=None,
        attempts=envelope.attempt,
        category=FailureCategory.PROVIDER_ERROR,
        enqueued_at=envelope.enqueued_at,
        first_attempted_at=NOW,
        last_attempted_at=NOW,
        dead_lettered_at=NOW,
        body=envelope.body,
    )


async def _reserved(queue: AgentQueue, body: str) -> tuple[str, JobEnvelope]:
    await queue.enqueue_body(body)
    raw = await queue.reserve(wait_seconds=1)
    assert raw is not None, "the job must actually be reserved, or nothing below is tested"
    return raw, JobEnvelope.decode(raw)


async def test_the_real_script_performs_the_whole_dead_letter_transition(
    queue: AgentQueue, redis: Redis
) -> None:
    """All four effects, from one round trip, interpreted by Redis itself."""
    raw, envelope = await _reserved(queue, '{"probe":"dead"}')

    assert await queue.dead_letter(raw, _record(queue, envelope)) is True

    assert await queue.inflight_depth() == 0
    assert await redis.hlen(f"{queue.namespace}:reservations") == 0
    assert await queue.failed_depth() == 1
    assert await queue.depth() == 0


async def test_a_second_dead_letter_for_one_reservation_writes_one_record(
    queue: AgentQueue,
) -> None:
    """The deduplication, and the reason the claim is the script's first line.

    `LREM` answering 0 makes the script return before it pushes anything, so a
    retry of the dead-letter path itself cannot double an entry an operator
    counts.
    """
    raw, envelope = await _reserved(queue, '{"probe":"twice"}')

    assert await queue.dead_letter(raw, _record(queue, envelope)) is True
    assert await queue.dead_letter(raw, _record(queue, envelope)) is False

    assert await queue.failed_depth() == 1


async def test_the_real_script_trims_to_the_retention_limit(
    queue: AgentQueue, redis: Redis
) -> None:
    """`LTRIM` with negative indices, evaluated by Redis rather than emulated.

    Seeded past the limit directly, because reaching it through the queue would
    mean a thousand reservations. What is under test is the trim arithmetic the
    script performs, and getting a sign wrong there would silently keep the
    *oldest* records - the opposite of what an incident needs.
    """
    failed = f"{queue.namespace}:failed"
    await redis.rpush(failed, *[f"old-{index}" for index in range(DEAD_LETTER_LIMIT)])

    raw, envelope = await _reserved(queue, '{"probe":"trim"}')
    assert await queue.dead_letter(raw, _record(queue, envelope)) is True

    assert await queue.failed_depth() == DEAD_LETTER_LIMIT
    newest = await redis.lindex(failed, -1)
    # The body is JSON inside JSON, so the quotes in it are escaped.
    assert newest is not None and "trim" in newest
    # The oldest went, not the newest.
    assert await redis.lindex(failed, 0) == "old-1"


async def test_the_real_script_performs_the_whole_retry_transition(
    queue: AgentQueue, redis: Redis
) -> None:
    """The sharper half: this one loses the job if it half-applies."""
    raw, _ = await _reserved(queue, '{"probe":"retry"}')

    assert (
        await queue.schedule_retry(
            raw,
            JobEnvelope.decode(raw),
            category=FailureCategory.DEPENDENCY_UNAVAILABLE,
            delay_seconds=30.0,
            now=NOW,
        )
        is True
    )

    assert await queue.inflight_depth() == 0
    assert await redis.hlen(f"{queue.namespace}:reservations") == 0
    assert await queue.delayed_depth() == 1

    scheduled = await redis.zrange(f"{queue.namespace}:delayed", 0, -1, withscores=True)
    entry, score = scheduled[0]
    assert JobEnvelope.decode(entry).attempt == 2
    assert score == pytest.approx(NOW.timestamp() + 30.0)


async def test_a_second_retry_for_one_reservation_schedules_once(queue: AgentQueue) -> None:
    """A job cannot be rescheduled twice off one claim, so it cannot fork."""
    raw, envelope = await _reserved(queue, '{"probe":"retry-twice"}')

    first = await queue.schedule_retry(
        raw, envelope, category=FailureCategory.TIMEOUT, delay_seconds=5.0, now=NOW
    )
    second = await queue.schedule_retry(
        raw, envelope, category=FailureCategory.TIMEOUT, delay_seconds=5.0, now=NOW
    )

    assert (first, second) == (True, False)
    assert await queue.delayed_depth() == 1


async def test_a_transition_that_never_reached_redis_leaves_the_job_recoverable(
    queue: AgentQueue, redis: Redis
) -> None:
    """The failure the scripts exist for, against a Redis that is not there.

    A second queue over the same namespace, on a client pointed at a closed
    port: its `dead_letter` raises before anything runs, and the entry is still
    in flight where a reaper will find it. The old four-round-trip version could
    fail *between* its steps and leave the job terminal with no record; this
    cannot, because there is no between.

    `aclose()` on the live client would not do - redis-py reconnects
    transparently on the next command, so the call would simply succeed and the
    test would prove nothing.
    """
    raw, envelope = await _reserved(queue, '{"probe":"unreachable"}')
    assert await queue.inflight_depth() == 1

    unreachable = AgentQueue(
        Redis.from_url("redis://127.0.0.1:6399/0", decode_responses=True, socket_connect_timeout=1),
        namespace=queue.namespace,
        visibility_timeout_seconds=60,
    )
    with pytest.raises(RedisError):
        await unreachable.dead_letter(raw, _record(queue, envelope))

    assert await queue.inflight_depth() == 1
    assert await queue.failed_depth() == 0
    assert await redis.hlen(f"{queue.namespace}:reservations") == 1

    # And it is genuinely still finishable by somebody who can reach Redis.
    assert await queue.dead_letter(raw, _record(queue, envelope)) is True
    assert await queue.failed_depth() == 1
