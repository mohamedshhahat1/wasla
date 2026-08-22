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
from app.workers.queue import QUEUE_NAMESPACE, MalformedJobError

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
MEDIA = uuid.UUID("33333333-3333-3333-3333-333333333333")

PENDING = "media:understanding:pending"
INFLIGHT = "media:understanding:inflight"
FAILED = "media:understanding:failed"


class FakeRedis:
    def __init__(self, reserved=None):
        self.pushed = []
        self.removed = []
        self.moves = []
        self.lengths = {}
        self._reserved = reserved

    async def rpush(self, key, value):
        self.pushed.append((key, value))
        return 1

    # ASYNC109 wants asyncio.timeout, but this mirrors redis-py's own
    # signature: the fake has to accept what the queue actually passes.
    async def blmove(self, source, destination, timeout):  # noqa: ASYNC109
        self.moves.append((source, destination, timeout))
        return self._reserved

    async def lrem(self, key, count, value):
        self.removed.append((key, count, value))
        return 1

    async def llen(self, key):
        return self.lengths.get(key, 0)


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


async def test_enqueue_pushes_onto_the_pending_list():
    redis = FakeRedis()

    await MediaQueue(redis).enqueue(MediaJob(tenant_id=TENANT, media_id=MEDIA))

    key, value = redis.pushed[0]
    assert key == PENDING
    assert MediaJob.decode(value).media_id == MEDIA


async def test_reserving_moves_the_job_to_the_in_flight_list():
    """A worker killed mid-job must leave the job recoverable."""
    payload = MediaJob(tenant_id=TENANT, media_id=MEDIA).encode()
    redis = FakeRedis(reserved=payload)

    reserved = await MediaQueue(redis).reserve(wait_seconds=3)

    assert reserved == payload
    assert redis.moves == [(PENDING, INFLIGHT, 3)]


async def test_reserving_an_empty_queue_returns_nothing():
    redis = FakeRedis(reserved=None)

    assert await MediaQueue(redis).reserve() is None


async def test_releasing_removes_the_exact_payload():
    payload = MediaJob(tenant_id=TENANT, media_id=MEDIA).encode()
    redis = FakeRedis()

    await MediaQueue(redis).release(payload)

    assert redis.removed == [(INFLIGHT, 1, payload)]


async def test_failing_dead_letters_rather_than_discarding():
    """The job records that an attempt was made; the row records why it broke."""
    payload = MediaJob(tenant_id=TENANT, media_id=MEDIA).encode()
    redis = FakeRedis()

    await MediaQueue(redis).fail(payload)

    assert redis.removed == [(INFLIGHT, 1, payload)]
    assert redis.pushed == [(FAILED, payload)]


async def test_depths_report_the_two_lists():
    redis = FakeRedis()
    redis.lengths = {PENDING: 5, FAILED: 1}
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
