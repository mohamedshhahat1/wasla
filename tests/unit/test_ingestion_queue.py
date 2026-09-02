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
from app.workers.queue import QUEUE_NAMESPACE, DeadLetterRecord, JobEnvelope, MalformedJobError
from app.workers.retry import FailureCategory
from tests.fake_queue_redis import FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT = uuid.UUID("22222222-2222-2222-2222-222222222222")

PENDING = "knowledge:ingestion:pending"
INFLIGHT = "knowledge:ingestion:inflight"
FAILED = "knowledge:ingestion:failed"


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


async def test_enqueue_pushes_an_envelope_onto_the_pending_list():
    redis = FakeQueueRedis()

    await IngestionQueue(redis).enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))

    (value,) = redis.lists[PENDING]
    assert IngestionJob.decode(JobEnvelope.decode(value).body).document_id == DOCUMENT


async def test_reserving_moves_the_job_to_the_in_flight_list():
    """A worker killed mid-job must leave the job recoverable."""
    redis = FakeQueueRedis()
    queue = IngestionQueue(redis)
    await queue.enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))

    reserved = await queue.reserve(wait_seconds=3)

    assert reserved is not None
    assert redis.lists[INFLIGHT] == [reserved]
    assert redis.lists[PENDING] == []


async def test_reserving_an_empty_queue_returns_nothing():
    assert await IngestionQueue(FakeQueueRedis()).reserve() is None


async def test_releasing_removes_the_exact_payload():
    redis = FakeQueueRedis()
    queue = IngestionQueue(redis)
    await queue.enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))
    reserved = await queue.reserve()

    await queue.release(reserved)

    assert redis.lists[INFLIGHT] == []


async def test_a_transient_failure_is_retried_rather_than_dead_lettered():
    """Re-ingesting replaces a document's chunks, so another attempt is free."""
    redis = FakeQueueRedis()
    queue = IngestionQueue(redis)
    await queue.enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))
    reserved = await queue.reserve()

    await queue.schedule_retry(
        reserved,
        JobEnvelope.decode(reserved),
        category=FailureCategory.PROVIDER_ERROR,
        delay_seconds=2.0,
    )

    assert await queue.delayed_depth() == 1
    assert await queue.failed_depth() == 0


async def test_dead_lettering_records_rather_than_discarding():
    """The job records that an attempt was made; the document records why it broke."""
    redis = FakeQueueRedis()
    queue = IngestionQueue(redis)
    await queue.enqueue(IngestionJob(tenant_id=TENANT, document_id=DOCUMENT))
    reserved = await queue.reserve()
    envelope = JobEnvelope.decode(reserved)

    written = await queue.dead_letter(
        reserved,
        DeadLetterRecord(
            queue=INGESTION_NAMESPACE,
            job_type="ingestion",
            tenant_id=str(TENANT),
            job_id=str(DOCUMENT),
            attempts=envelope.attempt,
            category=FailureCategory.INVALID_REQUEST,
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
    redis.lists = {PENDING: ["a"] * 4, FAILED: ["b"] * 2}
    queue = IngestionQueue(redis)

    assert await queue.depth() == 4
    assert await queue.failed_depth() == 2
