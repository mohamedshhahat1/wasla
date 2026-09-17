"""Document indexing through the real, committing worker - every outcome bounded.

The audit's RAG-01 test used a flush-only session, so it could not see that the
worker's own transaction rolled `FAILED` back. Everything here goes through
`IngestionWorker` and `IngestionRecoveryWorker` against real PostgreSQL and real
Redis, with the embeddings provider faked at the HTTP transport, and reads state
back on a *different* connection - so what is asserted is what was committed.

The properties, each with the presence control that makes its absence meaningful:

* a permanent failure is `FAILED` after one attempt and the recovery sweep, which
  demonstrably claims other work, never touches it again (RAG-01);
* a transient failure is retried on a backoff, converges when the provider
  recovers, and ends `retry_exhausted` - not an ageing `pending` - when it does not;
* two deliveries of one job cost one embedding chain (RAG-09);
* no transaction is open while the provider thinks, so a delete commits while the
  embedding call is still in flight, and the late worker publishes nothing (RAG-04);
* a stale claim cannot publish, and an abandoned claim converges (crash recovery);
* suspended and deleted workspaces spend nothing, and a suspended one resumes (RAG-10);
* a re-index keeps the previous generation served until an atomic swap, and a
  failed re-index keeps it served for good (PD-RAG-1);
* a document embedded in another space is excluded and can be re-indexed (RAG-06);
* a burst of re-index requests is one re-index (RAG-07), and its cost is metered.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    DocumentIndexGeneration,
    DocumentStatus,
    GenerationState,
)
from app.db.models.usage import UsageEvent
from app.db.session import Database
from app.repositories.knowledge_repository import (
    TRIGGER_STALE_EMBEDDING,
    DocumentChunkRepository,
    IndexingSweep,
)
from app.services.document_indexing import (
    MAX_INDEXING_ATTEMPTS,
    DocumentIndexer,
    IndexingOutcome,
    committed_units,
    prepare,
    run_indexing,
)
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import IngestionJob
from tests.fake_embeddings import FAKE_MODEL
from tests.integration.ai_harness import JsonObject, embedding_response
from tests.integration.rag_harness import (
    LARGE,
    SMALL,
    Handler,
    Indexing,
    long_document,
    respond,
    vectors_of,
)

pytestmark = pytest.mark.integration

# ------------------------------------------------------------- RAG-01 permanent


PERMANENT = [
    ("401", respond(401), "provider_unauthorized"),
    ("403", respond(403), "provider_forbidden"),
    ("404_model_not_found", respond(404), "provider_model_not_found"),
    ("400_invalid_dimensions", respond(400), "provider_invalid_request"),
    ("422", respond(422), "provider_invalid_request"),
    ("width_minus_one", vectors_of(width=EMBEDDING_DIMENSIONS - 1), "invalid_embedding"),
    ("nan", vectors_of(math.nan), "invalid_embedding"),
    ("infinity", vectors_of(math.inf), "invalid_embedding"),
    ("null", vectors_of(None), "invalid_embedding"),
    ("bool", vectors_of(True), "invalid_embedding"),
    ("string", vectors_of("0.5"), "invalid_embedding"),
    ("zero", vectors_of(0.0), "invalid_embedding"),
]


@pytest.mark.parametrize(("case", "handler", "code"), PERMANENT, ids=[c[0] for c in PERMANENT])
async def test_a_permanent_failure_is_terminal_and_never_recovered(
    indexing: Indexing, case: str, handler: Handler, code: str
) -> None:
    tenant_id = await indexing.workspace()
    indexing.provider.handler = handler
    broken = await indexing.submit(tenant_id)
    # A second document, committed but never enqueued: the positive control that
    # the recovery sweep genuinely runs and claims what it should.
    owed = await indexing.submit(
        tenant_id, "Opening hours\n\nWe open at nine every day.", enqueue=False
    )

    assert await indexing.drain() == 1
    calls = len(indexing.provider.requests)

    (generation,) = await indexing.generations(broken)
    document = await indexing.document(broken)
    assert document is not None
    assert (document.status, generation.state) == (DocumentStatus.FAILED, GenerationState.FAILED)
    assert generation.last_error_code == code
    assert generation.attempts == 1
    assert document.error and len(document.error) <= 500
    assert calls == 1, "a permanent failure is one request, not a retry budget"
    assert await indexing.dead() == 0, "the job was acknowledged, not dead-lettered"

    indexing.provider.handler = None
    outcome = await indexing.recovery().run_once(now=datetime.now(UTC) + timedelta(days=1))
    assert outcome.claimed == 1, "recovery ran, and claimed exactly the owed document"
    await indexing.drain()

    assert len(indexing.provider.requests) == calls + 1, "only the owed document was embedded"
    (still,) = await indexing.generations(broken)
    assert still.state is GenerationState.FAILED and still.attempts == 1
    owed_document = await indexing.document(owed)
    assert owed_document is not None and owed_document.status is DocumentStatus.READY
    (entry,) = await indexing.audit(tenant_id, AuditAction.KNOWLEDGE_DOCUMENT_INDEXING_FAILED)
    assert entry.actor_kind is AuditActorKind.SYSTEM
    assert entry.target_id == broken
    assert entry.meta is not None and entry.meta["code"] == code


# ------------------------------------------------------------- RAG-01 transient


async def test_a_transient_failure_retries_on_a_backoff_and_converges(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    indexing.provider.handler = respond(503)
    document_id = await indexing.submit(tenant_id)

    await indexing.drain()
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.PENDING
    assert generation.attempts == 1
    assert generation.last_error_code == "provider_unavailable"
    assert generation.next_retry_at is not None and generation.next_retry_at > datetime.now(UTC)
    assert len(indexing.provider.requests) == 3, "the client's own retries, once"

    # Not due yet: the sweep leaves it alone.
    assert (await indexing.recovery().run_once()).claimed == 0

    indexing.provider.handler = None
    await indexing.make_due(document_id)
    assert (await indexing.recovery().run_once()).queued == 1
    await indexing.drain()

    generations = await indexing.generations(document_id)
    assert [g.state for g in generations] == [GenerationState.ACTIVE]
    assert generations[0].attempts == 2
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY
    assert set(await indexing.chunk_generations(document_id)) == {generations[0].id}


async def test_a_transient_failure_that_persists_ends_explicitly(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    indexing.provider.handler = respond(503)
    document_id = await indexing.submit(tenant_id)

    await indexing.drain()
    for _ in range(MAX_INDEXING_ATTEMPTS - 1):
        await indexing.make_due(document_id)
        assert (await indexing.recovery().run_once()).queued == 1
        await indexing.drain()

    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.FAILED
    assert generation.last_error_code == "retry_exhausted"
    assert generation.attempts == MAX_INDEXING_ATTEMPTS
    requests = len(indexing.provider.requests)
    assert requests == MAX_INDEXING_ATTEMPTS * 3

    # And nothing brings it back on its own.
    for _ in range(3):
        outcome = await indexing.recovery().run_once(now=datetime.now(UTC) + timedelta(days=30))
        assert outcome.claimed == 0
    await indexing.drain()
    assert len(indexing.provider.requests) == requests


async def test_a_failed_document_is_retried_only_when_somebody_asks(indexing: Indexing) -> None:
    """FAILED -> explicit re-index starts a new attempt; the old reason is kept (M26)."""
    tenant_id = await indexing.workspace()
    indexing.provider.handler = respond(401)
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    (failed,) = await indexing.generations(document_id)
    assert failed.state is GenerationState.FAILED

    indexing.provider.handler = None
    await indexing.reindex(tenant_id, document_id)
    first, second = await indexing.generations(document_id)
    assert (first.state, second.state) == (GenerationState.FAILED, GenerationState.PENDING)
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.PENDING
    assert document.error is None

    await indexing.drain()

    first, second = await indexing.generations(document_id)
    assert first.state is GenerationState.FAILED
    assert first.last_error_code == "provider_unauthorized", "history is kept"
    assert second.state is GenerationState.ACTIVE
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY


# ------------------------------------------------------------- RAG-09 duplicate


async def test_a_duplicate_job_costs_one_embedding_chain(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.queue().enqueue(IngestionJob(tenant_id=tenant_id, document_id=document_id))
    assert await indexing.queued() == 2

    indexing.provider.gate = asyncio.Event()
    first_pool, second_pool = Database(indexing.settings), Database(indexing.settings)
    try:
        first = asyncio.create_task(indexing.worker(first_pool).run_once(wait_seconds=1))
        await asyncio.wait_for(indexing.provider.entered.wait(), timeout=10)

        # The second worker reaches the claim while the first is inside the call.
        assert await asyncio.wait_for(
            indexing.worker(second_pool).run_once(wait_seconds=1), timeout=10
        )
        assert not first.done()
        assert len(indexing.provider.requests) == 1

        indexing.provider.gate.set()
        assert await first
    finally:
        await first_pool.dispose()
        await second_pool.dispose()

    assert await indexing.queued() == 0, "both envelopes were consumed"
    assert len(indexing.provider.requests) == 1
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.ACTIVE and generation.attempts == 1


# ------------------------------------------------------- RAG-04 no lock held


async def test_a_delete_commits_while_the_embedding_call_is_in_flight(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    indexing.provider.gate = asyncio.Event()

    worker = asyncio.create_task(indexing.drain(budget=1))
    await asyncio.wait_for(indexing.provider.entered.wait(), timeout=10)

    async with indexing.database.engine.connect() as observer:
        idle_in_transaction = await observer.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            )
        )
    assert idle_in_transaction == 0, "no transaction is held open across the provider call"

    started = time.perf_counter()
    async with indexing.database.session() as session:
        await asyncio.wait_for(
            KnowledgeService(session=session, tenant_id=tenant_id).delete_document(document_id),
            timeout=5,
        )
    assert time.perf_counter() - started < 5
    assert not worker.done(), "the provider call was still in flight when the delete committed"
    assert await indexing.document(document_id) is None

    indexing.provider.gate.set()
    await worker

    assert len(indexing.provider.requests) == 1
    assert await indexing.document(document_id) is None
    assert await indexing.generations(document_id) == []
    assert await indexing.chunk_generations(document_id) == []


async def test_a_worker_whose_claim_was_taken_over_cannot_publish(indexing: Indexing) -> None:
    """A lease that lapsed, a second worker that re-claimed and published, a late first."""
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, enqueue=False)
    indexing.provider.gate = asyncio.Event()
    indexing.provider.gate_first_only = True

    late = asyncio.create_task(
        run_indexing(
            committed_units(indexing.database, tenant_id=tenant_id),
            document_id=document_id,
            embeddings=indexing.client(),
        )
    )
    await asyncio.wait_for(indexing.provider.entered.wait(), timeout=10)
    (held,) = await indexing.generations(document_id)
    first_token = held.claim_token

    await indexing.expire_lease(document_id)
    assert (await indexing.recovery().run_once()).queued == 1
    await indexing.drain()
    (published,) = await indexing.generations(document_id)
    assert published.state is GenerationState.ACTIVE and published.attempts == 2
    chunks_after_takeover = await indexing.chunk_generations(document_id)

    indexing.provider.gate.set()
    run = await late

    assert first_token is not None
    assert run.outcome is IndexingOutcome.STALE, "the late worker genuinely tried, and was refused"
    assert await indexing.chunk_generations(document_id) == chunks_after_takeover
    (still,) = await indexing.generations(document_id)
    assert still.state is GenerationState.ACTIVE and still.published_at == published.published_at


# ------------------------------------------------------------ crash recovery


async def test_a_claim_abandoned_by_a_dead_worker_converges(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, enqueue=False)
    async with indexing.database.session() as session:
        claim = await DocumentIndexer(session, tenant_id=tenant_id).claim(document_id, space=SMALL)
    assert not isinstance(claim, str)

    # Dead before any provider call. Held, so recovery leaves it for the lease.
    assert (await indexing.recovery().run_once()).claimed == 0
    await indexing.expire_lease(document_id)
    assert (await indexing.recovery().run_once()).queued == 1
    await indexing.drain()

    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.ACTIVE and generation.attempts == 2


async def test_a_worker_that_dies_after_embedding_leaves_nothing_half_published(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, long_document(), enqueue=False)
    units = committed_units(indexing.database, tenant_id=tenant_id)
    async with units() as indexer:
        claim = await indexer.claim(document_id, space=SMALL)
    assert not isinstance(claim, str)

    async def renew() -> bool:
        async with units() as indexer:
            return await indexer.renew(claim)

    async def meter(_batch: Any) -> None:
        return None

    prepared = await prepare(claim, indexing.client(), renew=renew, meter=meter)
    assert len(prepared.vectors) > 96 and len(indexing.provider.requests) >= 2
    # ...and the process dies here, before the publish.

    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.PROCESSING
    assert await indexing.chunk_generations(document_id) == []
    assert await indexing.search(tenant_id, "clause3w5") == []

    await indexing.expire_lease(document_id)
    assert (await indexing.recovery().run_once()).queued == 1
    await indexing.drain()

    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.ACTIVE
    assert len(await indexing.chunk_generations(document_id)) == len(prepared.pieces)


async def test_a_publish_that_fails_part_way_keeps_the_previous_generation_whole(
    indexing: Indexing, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    (v1,) = await indexing.generations(document_id)
    v1_chunks = await indexing.chunk_generations(document_id)
    await indexing.reindex(tenant_id, document_id)

    attempted: list[bool] = []

    async def explode(self: DocumentChunkRepository, *, generation_id: uuid.UUID) -> None:
        attempted.append(True)
        raise RuntimeError("the database went away mid-publish")

    monkeypatch.setattr(DocumentChunkRepository, "clear_for_generation", explode)
    await indexing.drain()

    assert attempted == [True], "the publish genuinely got as far as retiring v1"
    first, second = await indexing.generations(document_id)
    assert first.id == v1.id and first.state is GenerationState.ACTIVE
    assert second.state is GenerationState.FAILED
    assert await indexing.chunk_generations(document_id) == v1_chunks, "no half of v2 exists"
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY


# ------------------------------------------------------------ RAG-10 lifecycle


@pytest.mark.parametrize("change", ["suspended", "deleted"])
async def test_a_workspace_that_is_not_served_spends_nothing(
    indexing: Indexing, change: str
) -> None:
    tenant_id = await indexing.workspace()
    control_tenant = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    control = await indexing.submit(control_tenant, "Delivery\n\nWe deliver on Tuesdays.")
    if change == "suspended":
        await indexing.set_workspace(tenant_id, status=TenantStatus.SUSPENDED)
    else:
        await indexing.set_workspace(tenant_id, deleted=True)

    assert await indexing.drain() == 2, "both jobs were consumed"
    # Presence: the control workspace's document was embedded.
    assert len(indexing.provider.requests) == 1
    control_document = await indexing.document(control)
    assert control_document is not None and control_document.status is DocumentStatus.READY

    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.PENDING and generation.attempts == 0
    outcome = await indexing.recovery().run_once(now=datetime.now(UTC) + timedelta(days=1))
    assert outcome.claimed == 0

    if change == "suspended":
        assert generation.last_error_code == "workspace_suspended"
        await indexing.set_workspace(tenant_id, status=TenantStatus.ACTIVE)
        assert (await indexing.recovery().run_once()).queued == 1
        await indexing.drain()
        (resumed,) = await indexing.generations(document_id)
        assert resumed.state is GenerationState.ACTIVE


async def test_a_workspace_suspended_mid_ingestion_stops_at_the_next_batch(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, long_document())

    async def suspend_then_answer(body: JsonObject) -> httpx.Response:
        await indexing.set_workspace(tenant_id, status=TenantStatus.SUSPENDED)
        return embedding_response(body)

    indexing.provider.handler = suspend_then_answer
    await indexing.drain()

    assert len(indexing.provider.requests) == 1, "the second batch was never paid for"
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.PENDING
    assert generation.last_error_code == "workspace_suspended"
    assert await indexing.chunk_generations(document_id) == []


async def test_a_workspace_suspended_during_the_last_batch_cannot_publish(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)

    async def suspend_then_answer(body: JsonObject) -> httpx.Response:
        await indexing.set_workspace(tenant_id, status=TenantStatus.SUSPENDED)
        return embedding_response(body)

    indexing.provider.handler = suspend_then_answer
    await indexing.drain()

    assert len(indexing.provider.requests) == 1
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.PENDING
    assert await indexing.chunk_generations(document_id) == []
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.PENDING


# ------------------------------------------------------- last known good


async def test_a_reindex_serves_the_old_generation_until_an_atomic_swap(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    (v1,) = await indexing.generations(document_id)
    assert {g for g, _ in await indexing.search(tenant_id, "refund fourteen days")} == {v1.id}

    await indexing.reindex(tenant_id, document_id)
    indexing.provider.gate = asyncio.Event()
    worker = asyncio.create_task(indexing.drain(budget=1))
    await asyncio.wait_for(indexing.provider.entered.wait(), timeout=10)

    during = await indexing.search(tenant_id, "refund fourteen days")
    assert during and {g for g, _ in during} == {v1.id}, "v1 is served while v2 builds"
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY

    indexing.provider.gate.set()
    await worker

    first, second = await indexing.generations(document_id)
    assert (first.state, second.state) == (GenerationState.SUPERSEDED, GenerationState.ACTIVE)
    after = await indexing.search(tenant_id, "refund fourteen days")
    assert after and {g for g, _ in after} == {second.id}, "only v2 after the swap"
    assert set(await indexing.chunk_generations(document_id)) == {second.id}


async def test_a_failed_reindex_leaves_the_old_generation_serving(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    (v1,) = await indexing.generations(document_id)

    await indexing.reindex(tenant_id, document_id)
    indexing.provider.handler = respond(403)
    await indexing.drain()

    first, second = await indexing.generations(document_id)
    assert first.id == v1.id and first.state is GenerationState.ACTIVE
    assert second.state is GenerationState.FAILED
    assert second.last_error_code == "provider_forbidden"
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY
    assert {g for g, _ in await indexing.search(tenant_id, "refund fourteen days")} == {v1.id}


# ------------------------------------------------------- RAG-06 embedding space


async def test_a_model_change_excludes_old_vectors_until_they_are_reindexed(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    assert await indexing.search(tenant_id, "refund fourteen days", space=SMALL)

    indexing.configure(openai_embedding_model=LARGE.model)
    assert await indexing.search(tenant_id, "refund fourteen days", space=LARGE) == []
    async with indexing.database.session() as session:
        stale = await IndexingSweep(session).list_stale(space=LARGE, limit=10)
    assert [generation.document_id for generation in stale] == [document_id]

    await indexing.reindex(tenant_id, document_id, trigger=TRIGGER_STALE_EMBEDDING)
    await indexing.drain()

    assert indexing.provider.requests[-1]["model"] == LARGE.model
    assert await indexing.search(tenant_id, "refund fourteen days", space=LARGE)
    assert await indexing.search(tenant_id, "refund fourteen days", space=SMALL) == []
    async with indexing.database.session() as session:
        assert await IndexingSweep(session).list_stale(space=LARGE, limit=10) == []


# ------------------------------------------------------- RAG-07 cost


async def test_a_burst_of_reindex_requests_is_one_reindex(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    before = len(indexing.provider.requests)

    for _ in range(20):
        await indexing.reindex(tenant_id, document_id)
    generations = await indexing.generations(document_id)
    assert [g.state for g in generations] == [GenerationState.ACTIVE, GenerationState.PENDING]
    assert await indexing.queued() == 20, "every request was heard"

    assert await indexing.drain(budget=40) == 20
    assert len(indexing.provider.requests) == before + 1


async def test_ingestion_cost_is_metered_as_platform_cost_not_customer_turns(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id, long_document())
    await indexing.drain()

    batches = len(indexing.provider.requests)
    assert batches >= 2
    async with indexing.database.session() as session:
        rows = list(
            await session.scalars(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id))
        )
    requests = [row for row in rows if str(row.event_type) == "embedding_request"]
    tokens = [row for row in rows if str(row.event_type) == "embedding_input_token"]
    assert sum(row.quantity for row in requests) == batches
    assert all(row.meta and row.meta["purpose"] == "ingest" for row in requests)
    assert all(row.meta and row.meta["model"] == FAKE_MODEL for row in requests)
    assert sum(row.quantity for row in tokens) == 3 * batches
    assert not [row for row in rows if str(row.event_type) in {"ai_turn", "ai_request"}]


async def test_counts_of_documents_by_state_are_exact(indexing: Indexing) -> None:
    """The gauge source, against states this test put there itself."""
    tenant_id = await indexing.workspace()
    ready = await indexing.submit(tenant_id, "One\n\nFirst document.")
    await indexing.drain()
    indexing.provider.handler = respond(401)
    await indexing.submit(tenant_id, "Two\n\nSecond document.")
    await indexing.drain()
    indexing.provider.handler = respond(503)
    await indexing.submit(tenant_id, "Three\n\nThird document.")
    await indexing.drain()

    async with indexing.database.session() as session:
        rows = await session.execute(
            select(DocumentIndexGeneration.state, func.count())
            .where(DocumentIndexGeneration.tenant_id == tenant_id)
            .group_by(DocumentIndexGeneration.state)
        )
        counts: dict[GenerationState, int] = {state: int(total) for state, total in rows.all()}
    assert counts == {
        GenerationState.ACTIVE: 1,
        GenerationState.FAILED: 1,
        GenerationState.PENDING: 1,
    }
    assert (await indexing.document(ready)) is not None
