"""What happens to a document the queue refused, and who owns it afterwards.

`KnowledgeService._enqueue` logs a `RedisError` and swallows it, which is the
right trade: the document is committed and `PENDING`, which is the truth, and
failing the request would discard a file the customer successfully uploaded
because Redis was busy for a moment. Its docstring said `list_pending` existed
"so a sweeper can find anything stranded".

There was no sweeper, no gauge, no alert and no operator command, so a document
could sit `PENDING` for ever with nobody told (WQ-03). These are the four
properties that close it:

* a document the queue refused is found and re-queued;
* however many sweeps publish, the document converges on one indexed state;
* a document already indexed is left alone;
* a job lost *after* a successful publish - the `appendfsync everysec` window on
  an unclean host crash - is recovered from the durable row just the same.

Real PostgreSQL and real Redis. A broken queue is a client pointed at a closed
port rather than a mock, because what is under test is the behaviour of the code
around a `RedisError`, and a mock that raises one is a test of the mock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
)
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import INGESTION_NAMESPACE, IngestionJob, IngestionQueue
from app.workers.ingestion_recovery import IngestionRecoveryWorker
from app.workers.queue import JobEnvelope
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings

pytestmark = pytest.mark.integration


async def _int(result: Awaitable[int] | int) -> int:
    """Narrow a redis-py command result.

    One class backs both the sync and async clients, so every command is typed
    sync-or-async. The same narrowing `app.workers.queue._command` makes.
    """
    return await cast("Awaitable[int]", result)


async def _text(result: Awaitable[str | None] | str | None) -> str | None:
    return await cast("Awaitable[str | None]", result)


REDIS_URL = "redis://localhost:6379/14"
# A port nothing is listening on. Every command against it raises `RedisError`,
# which is what a Redis outage looks like from inside an upload request.
DEAD_REDIS_URL = "redis://127.0.0.1:6399/0"
PENDING = f"{INGESTION_NAMESPACE}:pending"


@pytest.fixture
async def live_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def broken_queue() -> IngestionQueue:
    """A queue that cannot be reached, which is the whole scenario."""
    return IngestionQueue(
        Redis.from_url(DEAD_REDIS_URL, decode_responses=True, socket_connect_timeout=1),
        visibility_timeout_seconds=60,
    )


@pytest.fixture
async def sweep_database(prepared_database: str) -> AsyncIterator[Database]:
    """A pool of the sweeper's own, because it commits on its own connections."""
    database = Database(_settings(prepared_database))
    try:
        yield database
    finally:
        await database.dispose()


def _settings(url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=url,
        redis_url=REDIS_URL,
    )


def _worker(database: Database, *, grace_seconds: float = 0.0) -> IngestionRecoveryWorker:
    settings = _settings(str(database.engine.url))
    return IngestionRecoveryWorker(
        database=database,
        redis=RedisClient(settings),
        settings=settings,
        # Zero grace by default, so a test need not wait five minutes for a
        # document it created a moment ago to become claimable.
        grace_seconds=grace_seconds,
    )


async def _workspace(session: AsyncSession) -> tuple[Tenant, KnowledgeBase]:
    tenant = Tenant(name="Ingest", slug=f"ingest-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    base = KnowledgeBase(tenant_id=tenant.id, name="Handbook")
    session.add(base)
    await session.flush()
    return tenant, base


async def _queued(redis: Redis) -> int:
    return await _int(redis.llen(PENDING))


async def _status(database: Database, document_id: uuid.UUID) -> DocumentStatus:
    async with database.session() as session:
        return (
            await session.execute(select(Document.status).where(Document.id == document_id))
        ).scalar_one()


async def _chunks(database: Database, document_id: uuid.UUID) -> int:
    async with database.session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
            )
        ).scalar_one()


async def _cleanup(database: Database, tenant_id: uuid.UUID) -> None:
    """Remove what these tests committed; deleting the workspace cascades."""
    async with database.session() as session:
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def test_a_document_the_queue_refused_is_found_and_requeued(
    sweep_database: Database,
    live_redis: Redis,
    broken_queue: IngestionQueue,
) -> None:
    """The end of WQ-03, measured: uploaded during an outage, indexed after it.

    Committed on the worker's own pool rather than staged in the rolled-back
    session fixture, because the sweeper opens its own connections and cannot
    see an uncommitted row.
    """
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        service = KnowledgeService(session=session, tenant_id=tenant_id, queue=broken_queue)
        document, created = await service.submit(
            knowledge_base_id=base.id,
            title="Refund policy",
            raw="Refunds are issued within fourteen days.",
        )
        document_id = document.id
        assert created

    try:
        # Non-vacuity: the upload really did fail to publish, so what follows is
        # recovery rather than a queue entry the upload left behind.
        assert await _queued(live_redis) == 0
        assert await _status(sweep_database, document_id) is DocumentStatus.PENDING

        outcome = await _worker(sweep_database).run_once()

        assert outcome.claimed == 1
        assert outcome.queued == 1
        assert outcome.still_owing == 0
        assert await _queued(live_redis) == 1

        head = await _text(live_redis.lindex(PENDING, 0))
        assert head is not None
        job = IngestionJob.decode(JobEnvelope.decode(head).body)
        assert job.document_id == document_id
        assert job.tenant_id == tenant_id
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_a_job_lost_after_a_successful_publish_is_recovered_too(
    sweep_database: Database,
    live_redis: Redis,
) -> None:
    """The other half of `appendfsync everysec`, which is why this worker exists.

    An unclean host crash can lose up to a second of Redis writes, including an
    ingestion job that was genuinely published. Agent and media work is covered
    because `InboundRecoveryWorker` re-derives it from `whatsapp_events`; this
    is what covers the third queue. The document's own row is the durable state,
    so losing the queue entry is recoverable from it.
    """
    working = IngestionQueue(live_redis, visibility_timeout_seconds=60)
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        document, _ = await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=working
        ).submit(
            knowledge_base_id=base.id,
            title="Opening hours",
            raw="We open at nine.",
        )
        document_id = document.id

    try:
        assert await _queued(live_redis) == 1, "the publish must genuinely have succeeded first"

        # The unclean crash: the write is gone, the row is not.
        await live_redis.delete(PENDING)
        assert await _queued(live_redis) == 0
        assert await _status(sweep_database, document_id) is DocumentStatus.PENDING

        outcome = await _worker(sweep_database).run_once()

        assert outcome.queued == 1
        assert await _queued(live_redis) == 1
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_two_sweepers_converge_on_one_indexed_document(
    sweep_database: Database,
    live_redis: Redis,
    broken_queue: IngestionQueue,
) -> None:
    """Deployments run more than one worker container, and both will sweep.

    The honest shape of this guarantee is worth stating, because it is weaker
    than the inbound sweeper's and deliberately so. `SKIP LOCKED` divides work
    between sweeps that genuinely *overlap* - proved directly in the test below,
    with one transaction held open across another's claim. It does not stop a
    second sweep some seconds later re-claiming the same document, because a
    document stays `PENDING` until the *consumer* indexes it, and the sweeper
    marks nothing: the document's own status is the durable state, and a sweeper
    that maintained a second one beside it would be a second thing to keep true
    and a new way to strand a row.

    So the property that has to hold is convergence rather than a single
    publication, and that is what is asserted here: however many envelopes the
    sweeps produce, the document ends `READY` exactly once, with one set of
    chunks rather than two. Re-ingestion clears chunks before writing new ones,
    and a `READY` document is left alone, which is what makes the extra envelope
    cost a round trip and nothing else.
    """
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        document, _ = await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=broken_queue
        ).submit(
            knowledge_base_id=base.id,
            title="Delivery",
            raw="We deliver on Tuesdays. Orders placed on Monday arrive that week.",
        )
        document_id = document.id

    try:
        first, second = await asyncio.gather(
            _worker(sweep_database).run_once(),
            _worker(sweep_database).run_once(),
        )
        envelopes = await _queued(live_redis)
        assert envelopes >= 1, "at least one sweep must have published something"
        assert first.queued + second.queued == envelopes

        # Every envelope, through the real consumer, counting chunks as we go.
        counts: list[int] = []
        embeddings = FakeEmbeddings()
        queue = IngestionQueue(live_redis, visibility_timeout_seconds=60)
        for _ in range(envelopes):
            raw = await queue.reserve(wait_seconds=1)
            assert raw is not None
            job = IngestionJob.decode(JobEnvelope.decode(raw).body)
            async with sweep_database.session() as session:
                await KnowledgeService(session=session, tenant_id=job.tenant_id).ingest(
                    document_id=job.document_id,
                    embeddings=as_embeddings(embeddings),
                )
            counts.append(await _chunks(sweep_database, document_id))

        assert await _status(sweep_database, document_id) is DocumentStatus.READY
        assert counts[0] > 0, "the first ingestion must genuinely have written chunks"
        # The document's final state is the same whichever envelope arrived
        # last: the second run finds it `READY` and leaves it alone, and a run
        # that did index again would replace the chunks rather than double them.
        assert len(set(counts)) == 1, f"the chunk count moved between envelopes: {counts}"
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_a_claimed_document_is_invisible_to_another_sweeper_holding_it_open(
    sweep_database: Database,
    broken_queue: IngestionQueue,
) -> None:
    """The lock itself, held open across a second sweeper's claim.

    The two-sweeper test above drives the whole worker, and two `run_once` calls
    can finish one after the other without ever overlapping - so it would pass
    whether or not the claim locks anything. This makes the overlap explicit:
    the first transaction claims and *stays open*, and only then does the second
    look.
    """
    from app.repositories.knowledge_repository import PendingDocumentSweep

    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        await KnowledgeService(session=session, tenant_id=tenant_id, queue=broken_queue).submit(
            knowledge_base_id=base.id,
            title="Warranty",
            raw="Two years.",
        )

    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=1)
        async with sweep_database.session() as first:
            claimed_by_first = await PendingDocumentSweep(first).claim_pending(
                older_than=cutoff, limit=10
            )
            assert len(claimed_by_first) == 1

            # The first transaction is still open and still holding the row.
            async with sweep_database.session() as second:
                claimed_by_second = await PendingDocumentSweep(second).claim_pending(
                    older_than=cutoff, limit=10
                )
            assert claimed_by_second == []
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_a_document_already_indexed_is_never_requeued(
    sweep_database: Database,
    live_redis: Redis,
    broken_queue: IngestionQueue,
) -> None:
    """A sweeper that re-queued finished work would cost embeddings for ever."""
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        document, _ = await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=broken_queue
        ).submit(
            knowledge_base_id=base.id,
            title="Returns",
            raw="Returns within thirty days.",
        )
        document.status = DocumentStatus.READY

    try:
        outcome = await _worker(sweep_database).run_once()
        assert outcome.claimed == 0
        assert await _queued(live_redis) == 0
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_a_document_still_being_published_is_not_raced(
    sweep_database: Database,
    live_redis: Redis,
    broken_queue: IngestionQueue,
) -> None:
    """Why the grace period is not decoration.

    A document committed a second ago has not failed: the request that committed
    it is very likely publishing its job at that exact moment. Sweeping it would
    race the path this worker exists to back up, and the honest cost of not
    racing it is that a genuinely stranded document waits one grace period.
    """
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        await KnowledgeService(session=session, tenant_id=tenant_id, queue=broken_queue).submit(
            knowledge_base_id=base.id,
            title="Terms",
            raw="The usual terms.",
        )

    try:
        fresh = await _worker(sweep_database, grace_seconds=300.0).run_once()
        assert fresh.claimed == 0
        assert await _queued(live_redis) == 0

        # The same document, once the grace period has passed.
        later = datetime.now(UTC) + timedelta(seconds=600)
        aged = await _worker(sweep_database, grace_seconds=300.0).run_once(now=later)
        assert aged.claimed == 1
        assert aged.queued == 1
    finally:
        await _cleanup(sweep_database, tenant_id)


async def test_a_redis_that_is_still_refusing_leaves_the_document_claimable(
    sweep_database: Database,
    broken_queue: IngestionQueue,
) -> None:
    """A sweep during a continuing outage must not mark anything done.

    The document's own status is the durable state and the ingestion worker is
    what advances it, so a sweep that could not publish simply reports the debt
    and leaves the row exactly as it found it - to be claimed again next pass.
    """
    async with sweep_database.session() as session:
        tenant, base = await _workspace(session)
        tenant_id = tenant.id
        document, _ = await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=broken_queue
        ).submit(
            knowledge_base_id=base.id,
            title="Pricing",
            raw="Prices on request.",
        )
        document_id = document.id

    try:
        worker = _worker(sweep_database)
        # The sweeper's queue is pointed at the closed port too, so this pass
        # meets the same outage the upload did.
        worker._queue = broken_queue

        outcome = await worker.run_once()

        assert outcome.claimed == 1
        assert outcome.queued == 0
        assert outcome.still_owing == 1
        assert await _status(sweep_database, document_id) is DocumentStatus.PENDING
    finally:
        await _cleanup(sweep_database, tenant_id)
