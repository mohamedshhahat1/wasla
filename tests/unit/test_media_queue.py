"""The media understanding queue.

Same shape as the other two queues, and the separation is asserted here for the
same reason it is there: a media job is a customer waiting for a reply, and it
must not queue behind a bulk document upload or compete with inference for the
same worker pool.
"""

import uuid

import pytest

from app.workers.ingestion_queue import INGESTION_NAMESPACE
from app.workers.media_queue import MEDIA_NAMESPACE, MediaJob, MediaQueue
from app.workers.queue import QUEUE_NAMESPACE, DeadLetterRecord, JobEnvelope, MalformedJobError
from app.workers.retry import FailureCategory
from tests.fake_queue_redis import FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
MEDIA = uuid.UUID("33333333-3333-3333-3333-333333333333")

PENDING = "media:understanding:pending"
INFLIGHT = "media:understanding:inflight"
FAILED = "media:understanding:failed"


def test_a_job_survives_a_round_trip():
    job = MediaJob(tenant_id=TENANT, media_id=MEDIA)

    assert MediaJob.decode(job.encode()) == job


def test_encoding_is_stable():
    """Releasing removes by exact value, so two encodings must match byte for byte."""
    first = MediaJob(tenant_id=TENANT, media_id=MEDIA).encode()
    second = MediaJob(tenant_id=TENANT, media_id=MEDIA).encode()

    assert first == second


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param(f'{{"tenant_id": "{TENANT}"}}', id="missing-media"),
        pytest.param(f'{{"tenant_id": "x", "media_id": "{MEDIA}"}}', id="bad-uuid"),
        pytest.param(f'{{"tenant_id": "{TENANT}", "media_id": 7}}', id="not-a-string"),
    ],
)
def test_a_malformed_job_is_refused(raw):
    """Retrying an unreadable job would fail identically forever."""
    with pytest.raises(MalformedJobError):
        MediaJob.decode(raw)


def test_media_has_its_own_queue():
    """Not the agent queue: a download pool is the wrong shape for inference.

    Not the ingestion queue either: a hundred uploaded documents must not sit in
    front of the photograph somebody is waiting for an answer about.
    """
    assert MEDIA_NAMESPACE not in (QUEUE_NAMESPACE, INGESTION_NAMESPACE)
    assert not PENDING.startswith(QUEUE_NAMESPACE)
    assert not PENDING.startswith(INGESTION_NAMESPACE)


async def test_enqueue_pushes_an_envelope_onto_the_pending_list():
    redis = FakeQueueRedis()

    await MediaQueue(redis).enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))

    (value,) = redis.lists[PENDING]
    assert MediaJob.decode(JobEnvelope.decode(value).body).media_id == MEDIA


async def test_reserving_moves_the_job_to_the_in_flight_list():
    """A worker killed mid-job must leave the job recoverable."""
    redis = FakeQueueRedis()
    queue = MediaQueue(redis)
    await queue.enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))

    reserved = await queue.reserve(wait_seconds=3)

    assert reserved is not None
    assert redis.lists[INFLIGHT] == [reserved]
    assert redis.lists[PENDING] == []


async def test_reserving_an_empty_queue_returns_nothing():
    assert await MediaQueue(FakeQueueRedis()).reserve() is None


async def test_releasing_removes_the_exact_payload():
    redis = FakeQueueRedis()
    queue = MediaQueue(redis)
    await queue.enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))
    reserved = await queue.reserve()

    await queue.release(reserved)

    assert redis.lists[INFLIGHT] == []


async def test_a_transient_failure_is_retried_rather_than_dead_lettered():
    """A file already stored is not fetched again, so another attempt is free."""
    redis = FakeQueueRedis()
    queue = MediaQueue(redis)
    await queue.enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))
    reserved = await queue.reserve()

    await queue.schedule_retry(
        reserved,
        JobEnvelope.decode(reserved),
        category=FailureCategory.DEPENDENCY_UNAVAILABLE,
        delay_seconds=2.0,
    )

    assert await queue.delayed_depth() == 1
    assert await queue.failed_depth() == 0


async def test_dead_lettering_records_rather_than_discarding():
    """The job records that an attempt was made; the row records why it broke."""
    redis = FakeQueueRedis()
    queue = MediaQueue(redis)
    await queue.enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))
    reserved = await queue.reserve()
    envelope = JobEnvelope.decode(reserved)

    written = await queue.dead_letter(
        reserved,
        DeadLetterRecord(
            queue=MEDIA_NAMESPACE,
            job_type="media",
            tenant_id=str(TENANT),
            job_id=str(MEDIA),
            attempts=envelope.attempt,
            category=FailureCategory.NOT_FOUND,
            enqueued_at=envelope.enqueued_at,
            first_attempted_at=None,
            last_attempted_at=envelope.enqueued_at,
            dead_lettered_at=envelope.enqueued_at,
            body=envelope.body,
        ),
    )

    assert written is True
    assert redis.lists[INFLIGHT] == []
    assert await queue.failed_depth() == 1


async def test_depths_report_every_list():
    redis = FakeQueueRedis()
    redis.lists = {PENDING: ["a"] * 5, FAILED: ["b"]}
    queue = MediaQueue(redis)

    assert await queue.depth() == 5
    assert await queue.failed_depth() == 1


def test_the_block_interval_leaves_room_under_the_socket_timeout():
    """The phase 8 bug, guarded.

    redis-py applies its read timeout to every read including a deliberate
    block, so a reserve that waits as long as the socket allows trips its own
    timeout and kills the worker.
    """
    from app.core.redis import BLOCKING_HEADROOM_SECONDS, MAX_BLOCKING_SECONDS
    from app.workers.media_queue import BLOCK_SECONDS

    assert BLOCK_SECONDS == MAX_BLOCKING_SECONDS
    assert BLOCKING_HEADROOM_SECONDS > 0
