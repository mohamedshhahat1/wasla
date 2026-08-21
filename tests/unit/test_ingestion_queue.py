"""The document ingestion queue.

The queue takes a raw Redis client, so these tests hand it a fake that records
the list commands it uses. The point of the separate queue is asserted here too:
its keys must not be the agent queue's, or a bulk upload would sit in front of a
customer's question.
"""

import uuid

import pytest

from app.workers.ingestion_queue import (
    INGESTION_NAMESPACE,
    IngestionJob,
    IngestionQueue,
)
from app.workers.queue import QUEUE_NAMESPACE, MalformedJobError

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT = uuid.UUID("22222222-2222-2222-2222-222222222222")

PENDING = "knowledge:ingestion:pending"
INFLIGHT = "knowledge:ingestion:inflight"
FAILED = "knowledge:ingestion:failed"


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
    job = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT)

    assert IngestionJob.decode(job.encode()) == job


def test_encoding_is_stable():
    """Releasing removes by exact value, so two encodings must match byte for byte."""
    first = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT).encode()
    second = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT).encode()

    assert first == second


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param(f'{{"tenant_id": "{TENANT}"}}', id="missing-document"),
        pytest.param(f'{{"tenant_id": "x", "document_id": "{DOCUMENT}"}}', id="bad-uuid"),
    ],
)
def test_a_malformed_job_is_refused(raw):
    """Retrying an unreadable job would fail identically forever."""
    with pytest.raises(MalformedJobError):
        IngestionJob.decode(raw)


async def test_ingestion_does_not_share_the_agent_queue():
    """A bulk upload must not sit in front of a customer waiting for a reply."""
    assert INGESTION_NAMESPACE != QUEUE_NAMESPACE
    assert not PENDING.startswith(QUEUE_NAMESPACE)


async def test_enqueue_pushes_onto_the_pending_list():
    redis = FakeRedis()

    await IngestionQueue(redis).enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))

    key, value = redis.pushed[0]
    assert key == PENDING
    assert IngestionJob.decode(value).document_id == DOCUMENT


async def test_reserving_moves_the_job_to_the_in_flight_list():
    """A worker killed mid-job must leave the job recoverable."""
    payload = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT).encode()
    redis = FakeRedis(reserved=payload)

    reserved = await IngestionQueue(redis).reserve(wait_seconds=3)

    assert reserved == payload
    assert redis.moves == [(PENDING, INFLIGHT, 3)]


async def test_reserving_an_empty_queue_returns_nothing():
    redis = FakeRedis(reserved=None)

    assert await IngestionQueue(redis).reserve() is None


async def test_releasing_removes_the_exact_payload():
    payload = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT).encode()
    redis = FakeRedis()

    await IngestionQueue(redis).release(payload)

    assert redis.removed == [(INFLIGHT, 1, payload)]


async def test_failing_dead_letters_rather_than_discarding():
    """The job records that an attempt was made; the document records why it broke."""
    payload = IngestionJob(tenant_id=TENANT, document_id=DOCUMENT).encode()
    redis = FakeRedis()

    await IngestionQueue(redis).fail(payload)

    assert redis.removed == [(INFLIGHT, 1, payload)]
    assert redis.pushed == [(FAILED, payload)]


async def test_depths_report_the_two_lists():
    redis = FakeRedis()
    redis.lengths = {PENDING: 4, FAILED: 2}
    queue = IngestionQueue(redis)

    assert await queue.depth() == 4
    assert await queue.failed_depth() == 2
