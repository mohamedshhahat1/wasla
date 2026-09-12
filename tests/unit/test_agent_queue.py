"""The agent job queue.

The queue takes a raw Redis client, so these tests hand it a fake that keeps
real lists and a real sorted set. That matters more than it used to: since the
queue grew retries and dead-letter records, several of its guarantees are
statements about what a *second* command returns, and a fake that answered
every removal with 1 could not tell a working implementation from a broken one.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.workers.ai_worker import AGENT_RETRY
from app.workers.queue import (
    DEAD_LETTER_LIMIT,
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    AgentJob,
    AgentQueue,
    DeadLetterRecord,
    JobEnvelope,
    MalformedJobError,
)
from app.workers.retry import FailureCategory
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_redis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
AGENT = uuid.UUID("33333333-3333-3333-3333-333333333333")

PENDING = "agent:jobs:pending"
INFLIGHT = "agent:jobs:inflight"
DELAYED = "agent:jobs:delayed"
FAILED = "agent:jobs:failed"

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def record(
    category: FailureCategory = FailureCategory.PROVIDER_ERROR,
    attempts: int = 1,
    body: str = "{}",
) -> DeadLetterRecord:
    return DeadLetterRecord(
        queue="agent:jobs",
        job_type="agent",
        tenant_id=str(TENANT),
        job_id=str(CONVERSATION),
        attempts=attempts,
        category=category,
        enqueued_at=NOW,
        first_attempted_at=NOW,
        last_attempted_at=NOW,
        dead_lettered_at=NOW,
        body=body,
    )


# ------------------------------------------------------------- the payload


def test_a_job_survives_a_round_trip() -> None:
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION, agent_id=AGENT)

    assert AgentJob.decode(job.encode()) == job


def test_a_job_without_a_chosen_agent_survives_a_round_trip() -> None:
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    decoded = AgentJob.decode(job.encode())

    assert decoded == job
    assert decoded.agent_id is None


def test_identical_jobs_encode_identically() -> None:
    """Releasing a job removes it by exact value, so encoding must be stable."""
    first = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)
    second = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    assert first.encode() == second.encode()


def test_encoded_keys_are_sorted() -> None:
    encoded = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode()

    assert encoded.index("conversation_id") < encoded.index("tenant_id")


def test_text_that_is_not_json_is_malformed() -> None:
    with pytest.raises(MalformedJobError):
        AgentJob.decode("not json at all")


def test_json_that_is_not_an_object_is_malformed() -> None:
    with pytest.raises(MalformedJobError):
        AgentJob.decode("[1, 2, 3]")


def test_a_missing_identifier_is_malformed() -> None:
    with pytest.raises(MalformedJobError):
        AgentJob.decode('{"tenant_id": "11111111-1111-1111-1111-111111111111"}')


def test_an_unusable_identifier_is_malformed() -> None:
    with pytest.raises(MalformedJobError):
        AgentJob.decode('{"tenant_id": "not-a-uuid", "conversation_id": "also-not"}')


# ------------------------------------------------------------ the envelope


def test_an_envelope_carries_the_payload_through_unchanged() -> None:
    """The body is opaque, so a job type's own encoding is never re-derived."""
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)
    envelope = JobEnvelope.wrap(job.encode(), now=NOW)

    decoded = JobEnvelope.decode(envelope.encode())

    assert decoded.body == job.encode()
    assert AgentJob.decode(decoded.body) == job


def test_a_fresh_envelope_is_attempt_one() -> None:
    assert JobEnvelope.wrap("{}", now=NOW).attempt == 1


def test_a_bare_payload_left_over_from_an_earlier_release_is_attempt_one() -> None:
    """A deploy must not strand jobs that were already sitting in the queue."""
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    envelope = JobEnvelope.decode(job.encode())

    assert envelope.attempt == 1
    assert AgentJob.decode(envelope.body) == job


def test_an_entry_that_is_not_json_at_all_becomes_a_body_rather_than_an_error() -> None:
    """Deciding an entry is unreadable belongs to the job decoder, not here."""
    envelope = JobEnvelope.decode("garbage")

    assert envelope.body == "garbage"
    with pytest.raises(MalformedJobError):
        AgentJob.decode(envelope.body)


def test_the_next_attempt_keeps_the_original_enqueue_time() -> None:
    """Queue age must measure how long the customer waited, not the last try."""
    first = JobEnvelope.wrap("{}", now=NOW)

    second = first.next_attempt(category=FailureCategory.TIMEOUT, now=NOW + timedelta(minutes=5))

    assert second.attempt == 2
    assert second.enqueued_at == NOW
    assert second.first_attempted_at == NOW + timedelta(minutes=5)
    assert second.last_failure is FailureCategory.TIMEOUT


def test_the_first_attempt_time_is_recorded_once() -> None:
    first = JobEnvelope.wrap("{}", now=NOW)
    second = first.next_attempt(category=FailureCategory.TIMEOUT, now=NOW + timedelta(minutes=1))

    third = second.next_attempt(category=FailureCategory.TIMEOUT, now=NOW + timedelta(minutes=9))

    assert third.first_attempted_at == NOW + timedelta(minutes=1)


# --------------------------------------------------------------- the queue


async def test_enqueue_appends_an_envelope_to_the_pending_list() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    await queue.enqueue(job, now=NOW)

    assert len(redis.lists[PENDING]) == 1
    assert JobEnvelope.decode(redis.lists[PENDING][0]).body == job.encode()


async def test_reserving_moves_the_job_to_the_in_flight_list() -> None:
    """Popping outright would lose the job if the worker died mid-turn."""
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)
    await queue.enqueue(job, now=NOW)

    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    assert raw is not None
    assert redis.lists[PENDING] == []
    assert redis.lists[INFLIGHT] == [raw]


async def test_reserving_returns_nothing_when_the_queue_is_quiet() -> None:
    queue = AgentQueue(as_redis(FakeQueueRedis()))

    assert await queue.reserve(wait_seconds=1, now=NOW) is None


async def test_releasing_removes_the_job_from_the_in_flight_list() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    await queue.release(raw)

    assert redis.lists[INFLIGHT] == []


async def test_a_job_a_worker_never_released_stays_recoverable() -> None:
    """The whole reason reserve moves rather than pops."""
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    # The worker dies here: no release, no fail.
    assert redis.lists[INFLIGHT] == [raw]


# ------------------------------------------------------------- retrying


async def test_a_retry_leaves_the_pending_list_and_lands_in_the_delayed_set() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None
    envelope = JobEnvelope.decode(raw)

    taken = await queue.schedule_retry(
        raw, envelope, category=FailureCategory.TIMEOUT, delay_seconds=30.0, now=NOW
    )

    assert taken is True
    assert redis.lists[INFLIGHT] == []
    assert redis.lists[PENDING] == []
    assert await queue.delayed_depth() == 1


async def test_a_retry_increments_the_attempt_and_records_the_category() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    await queue.schedule_retry(
        raw,
        JobEnvelope.decode(raw),
        category=FailureCategory.RATE_LIMITED,
        delay_seconds=5.0,
        now=NOW,
    )

    (scheduled,) = redis.zsets[DELAYED]
    envelope = JobEnvelope.decode(scheduled)
    assert envelope.attempt == 2
    assert envelope.last_failure is FailureCategory.RATE_LIMITED


async def test_a_retry_is_not_visible_before_its_moment() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None
    await queue.schedule_retry(
        raw,
        JobEnvelope.decode(raw),
        category=FailureCategory.TIMEOUT,
        delay_seconds=60.0,
        now=NOW,
    )

    assert await queue.reserve(wait_seconds=1, now=NOW + timedelta(seconds=59)) is None
    assert await queue.reserve(wait_seconds=1, now=NOW + timedelta(seconds=61)) is not None


async def test_promoting_twice_does_not_queue_the_same_retry_twice() -> None:
    """The `zrem` is the claim; a second promoter must find nothing."""
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None
    await queue.schedule_retry(
        raw,
        JobEnvelope.decode(raw),
        category=FailureCategory.TIMEOUT,
        delay_seconds=1.0,
        now=NOW,
    )
    later = NOW + timedelta(seconds=10)

    assert await queue.promote_due(now=later) == 1
    assert await queue.promote_due(now=later) == 0
    assert len(redis.lists[PENDING]) == 1


async def test_a_retry_a_worker_no_longer_holds_is_refused() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None
    envelope = JobEnvelope.decode(raw)
    await queue.release(raw)

    taken = await queue.schedule_retry(
        raw, envelope, category=FailureCategory.TIMEOUT, delay_seconds=1.0, now=NOW
    )

    assert taken is False
    assert await queue.delayed_depth() == 0


# ---------------------------------------------------------- dead-lettering


async def test_dead_lettering_records_the_job() -> None:
    """A failed job is the only evidence a customer went unanswered."""
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    assert await queue.dead_letter(raw, record()) is True

    assert redis.lists[INFLIGHT] == []
    (written,) = redis.lists[FAILED]
    assert json.loads(written)["category"] == "provider_error"


async def test_dead_lettering_the_same_reservation_twice_writes_one_record() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    first = await queue.dead_letter(raw, record())
    second = await queue.dead_letter(raw, record())

    assert (first, second) == (True, False)
    assert await queue.failed_depth() == 1


async def test_a_dead_letter_record_carries_what_an_operator_needs() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    await queue.dead_letter(raw, record(attempts=5))

    written = json.loads(redis.lists[FAILED][0])
    assert written["attempts"] == 5
    assert written["job_type"] == "agent"
    assert written["tenant_id"] == str(TENANT)
    assert written["job_id"] == str(CONVERSATION)
    assert {"enqueued_at", "first_attempted_at", "last_attempted_at", "dead_lettered_at"} <= set(
        written
    )


async def test_a_dead_letter_record_carries_no_exception_text() -> None:
    """The category is the whole vocabulary; a repr could leak anything."""
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    await queue.dead_letter(raw, record())

    written = json.loads(redis.lists[FAILED][0])
    assert set(written) <= {
        "attempts",
        "body",
        "category",
        "dead_lettered_at",
        "enqueued_at",
        "first_attempted_at",
        "job_id",
        "job_type",
        "last_attempted_at",
        "queue",
        "tenant_id",
    }


async def test_the_dead_letter_list_is_capped_and_keeps_the_newest() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    for index in range(DEAD_LETTER_LIMIT + 5):
        redis.lists.setdefault("agent:jobs:inflight", []).append(f"job-{index}")
        await queue.dead_letter(f"job-{index}", record(body=f"body-{index}"))

    assert await queue.failed_depth() == DEAD_LETTER_LIMIT
    newest = json.loads(redis.lists[FAILED][-1])
    assert newest["body"] == f"body-{DEAD_LETTER_LIMIT + 4}"


async def test_dead_letters_are_readable_newest_first() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    for index in range(3):
        redis.lists.setdefault("agent:jobs:inflight", []).append(f"job-{index}")
        await queue.dead_letter(f"job-{index}", record(body=f"body-{index}"))

    entries = await queue.dead_letters(limit=2)

    assert [json.loads(entry)["body"] for entry in entries] == ["body-2", "body-1"]


# ----------------------------------------------------------------- depths


async def test_depth_counts_only_waiting_jobs() -> None:
    redis = FakeQueueRedis()
    redis.lists[PENDING] = ["a", "b", "c"]
    redis.lists[FAILED] = ["x"] * 7
    redis.lists[INFLIGHT] = ["y"] * 2
    redis.zsets[DELAYED] = {"z": 1.0}
    queue = AgentQueue(as_redis(redis))

    assert await queue.depth() == 3
    assert await queue.failed_depth() == 7
    assert await queue.inflight_depth() == 2
    assert await queue.delayed_depth() == 1


async def test_the_oldest_pending_age_measures_the_head_of_the_queue() -> None:
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis))
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)
    await queue.enqueue(
        AgentJob(tenant_id=TENANT, conversation_id=AGENT), now=NOW + timedelta(minutes=4)
    )

    age = await queue.oldest_pending_age_seconds(now=NOW + timedelta(minutes=5))

    assert age == pytest.approx(300.0)


async def test_an_empty_queue_has_no_oldest_age_rather_than_a_zero_one() -> None:
    """Zero would read as "nothing is waiting long", which is a different claim."""
    assert await AgentQueue(as_redis(FakeQueueRedis())).oldest_pending_age_seconds(now=NOW) is None


# ----------------------------- byte-identical reservations fail safe, WQ-08


async def test_two_byte_identical_payloads_share_one_reservation_record() -> None:
    """The structural property, stated rather than discovered again.

    The reservation hash is keyed by the exact payload, so two in-flight copies
    of identical bytes share one record. Everything that follows from that is
    recorded here because it is a property of the data structure rather than a
    guarded invariant, and a future change that made it reachable should fail a
    test rather than surprise somebody (WQ-08).

    It is nearly unreachable today: `JobEnvelope.wrap` stamps `enqueued_at` at
    microsecond resolution, so two publications of one job differ. This test
    forces the collision by pinning that clock, which is the only way to reach
    it at all.
    """
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis), namespace="agent:jobs")

    body = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode()
    await queue.enqueue_body(body, now=NOW)
    await queue.enqueue_body(body, now=NOW)

    first = await queue.reserve(wait_seconds=1, now=NOW)
    second = await queue.reserve(wait_seconds=1, now=NOW)
    assert first == second, "the collision under test needs the two to be identical bytes"

    assert await queue.inflight_depth() == 2
    assert len(redis.strings[f"{INFLIGHT[: -len(":inflight")]}:reservations"]) == 1


async def test_the_orphan_of_a_shared_reservation_is_never_silently_repeated() -> None:
    """Why WQ-08 is closed by design rather than fixed.

    Releasing one copy removes the shared reservation, orphaning the other: an
    in-flight entry with no reservation record. That is not a new state - it is
    the same `UNKNOWN` stage a worker that died between `BLMOVE` and `HSET`
    leaves - and recovery already has an answer for it. On a queue that is not
    idempotent the answer is quarantine, so the orphan becomes a dead letter an
    operator reads rather than a second reply on a customer's phone.

    Every consequence of the collision therefore fails in the safe direction,
    which is why this is recorded and not redesigned. Since WQ-01 there is a
    second line under it as well: two envelopes for one customer message are one
    logical turn, so even a repeat that got past here would find the turn owned.
    """
    redis = FakeQueueRedis()
    queue = AgentQueue(as_redis(redis), namespace="agent:jobs")

    body = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode()
    await queue.enqueue_body(body, now=NOW)
    await queue.enqueue_body(body, now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    # One copy finishes, taking the shared reservation with it.
    assert await queue.release(raw) is True
    assert await queue.inflight_depth() == 1
    assert redis.strings.get(f"{INFLIGHT[: -len(":inflight")]}:reservations", {}) == {}

    # The orphan is *adopted* rather than stranded: an in-flight entry with no
    # reservation gets one starting now, so the next pass judges it by the same
    # rule every other entry is judged by. That costs one visibility timeout and
    # is why this first call recovers nothing.
    later = NOW + timedelta(seconds=1)
    assert await queue.recover_expired(policy=AGENT_RETRY, now=later) == []
    assert await queue.inflight_depth() == 1

    # One timeout later it is judged. `UNKNOWN` on a non-idempotent queue is not
    # safe to repeat, so it is quarantined rather than sent a second time.
    outcomes = await queue.recover_expired(
        policy=AGENT_RETRY,
        now=later + timedelta(seconds=DEFAULT_VISIBILITY_TIMEOUT_SECONDS + 1),
    )
    assert [outcome.action for outcome in outcomes] == ["quarantined"]
    assert [outcome.category for outcome in outcomes] == [FailureCategory.UNCERTAIN_DELIVERY]
    assert await queue.depth() == 0, "a second copy must never go back on the queue"
    assert await queue.failed_depth() == 1
