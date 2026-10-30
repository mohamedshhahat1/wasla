"""Faster ingestion without holding anything across a provider call (FX-4.8, IMP-20, P-R29).

Through the real worker on real PostgreSQL and Redis, embeddings faked at the
transport with a fixed latency: batches of one document overlap in time, a
claim renewal precedes every wave, vectors keep their chunks' order whatever
order the answers arrive in, a failed batch does not lose the metering of the
ones that came back, and the chunks of a generation go in one statement.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import event, func, select, text

import app.services.document_indexing as indexing_module
from app.db.models.knowledge import DocumentChunk, GenerationState
from app.db.models.usage import UsageEvent
from app.services.contextual_embedding import embedding_input
from app.services.retrieval_ranking import lexical_tokens
from tests.fake_embeddings import embed_text
from tests.integration.ai_harness import JsonObject, embedding_response
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

LATENCY = 0.2
# 320 chunks: four batches of 96.
BODY = "\n\n".join(
    f"Clause {n}. " + " ".join(f"term{n}x{w}" for w in range(60)) for n in range(320)
)


def _timed(
    indexing: Indexing, spans: list[tuple[float, float]], *, slow_first: bool = False
) -> None:
    async def handle(body: JsonObject) -> httpx.Response:
        started = time.perf_counter()
        first = len(spans) == 0
        spans.append((started, 0.0))
        index = len(spans) - 1
        await asyncio.sleep(LATENCY * (3 if slow_first and first else 1))
        spans[index] = (started, time.perf_counter())
        return embedding_response(body)

    indexing.provider.handler = handle


def _count_renewals(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    renewals: list[float] = []
    original = indexing_module.DocumentIndexer.renew

    async def renew(self: Any, claim: Any) -> bool:
        renewals.append(time.perf_counter())
        return await original(self, claim)

    monkeypatch.setattr(indexing_module.DocumentIndexer, "renew", renew)
    return renewals


async def test_a_documents_batches_are_embedded_together_under_one_renewal_per_wave(
    indexing: Indexing, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexing.configure(rag_embedding_concurrency=3)
    renewals = _count_renewals(monkeypatch)
    spans: list[tuple[float, float]] = []
    _timed(indexing, spans)
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, BODY)
    await indexing.drain()

    assert len(spans) == 4
    first_wave = spans[:3]
    # Really overlapped: each of the wave's requests started before the
    # first of them ended.
    assert max(start for start, _ in first_wave) < min(end for _, end in first_wave)
    # One renewal per wave, each before its wave's requests.
    assert len(renewals) == 2
    assert renewals[0] < spans[0][0] and spans[2][1] <= renewals[1] < spans[3][0]
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.ACTIVE and generation.chunk_count == 320


async def test_one_at_a_time_is_still_one_renewal_per_batch(
    indexing: Indexing, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexing.configure(rag_embedding_concurrency=1)
    renewals = _count_renewals(monkeypatch)
    spans: list[tuple[float, float]] = []
    _timed(indexing, spans)
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id, BODY)
    await indexing.drain()
    assert len(spans) == len(renewals) == 4
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(3))


async def test_vectors_keep_their_chunks_order_when_answers_arrive_out_of_order(
    indexing: Indexing,
) -> None:
    indexing.configure(rag_embedding_concurrency=3)
    spans: list[tuple[float, float]] = []
    _timed(indexing, spans, slow_first=True)
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, BODY)
    await indexing.drain()
    assert spans[0][1] > spans[1][1]  # the first batch answered last
    async with indexing.database.session() as session:
        chunks = list(
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.ordinal)
            )
        )
    for chunk in (chunks[0], chunks[100], chunks[200], chunks[-1]):
        expected = embed_text(
            embedding_input(
                chunk.content, title="Doc", heading_path=chunk.heading_path, filename=None
            )
        )
        assert chunk.embedding is not None
        assert list(chunk.embedding) == pytest.approx(expected, abs=1e-6)


async def test_a_batch_that_fails_does_not_lose_the_cost_of_the_ones_that_came_back(
    indexing: Indexing,
) -> None:
    indexing.configure(rag_embedding_concurrency=3)

    async def second_fails(body: JsonObject) -> httpx.Response:
        # The second batch - the one opening with chunk 96 - fails every attempt.
        if str(body["input"][0]).find("Clause 96.") >= 0:
            return httpx.Response(
                503, json={"error": {"code": "scripted"}}, headers={"retry-after": "0"}
            )
        return embedding_response(body)

    indexing.provider.handler = second_fails
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, BODY)
    await indexing.drain(budget=1)
    (generation,) = await indexing.generations(document_id)
    assert generation.state is GenerationState.PENDING  # transient: retried later
    async with indexing.database.session() as session:
        metered = await session.scalar(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.event_type == "embedding_request",
            )
        )
    # Batch two failed after its retries; batches one and three were paid for
    # and are on the meter.
    assert metered == 2
    assert await indexing.chunk_generations(document_id) == []


async def test_a_generations_chunks_go_in_one_statement_with_their_lexemes(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, BODY)
    inserts: list[int] = []

    def count(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO DOCUMENT_CHUNKS"):
            inserts.append(len(parameters) if executemany else 1)

    event.listen(indexing.database.engine.sync_engine, "before_cursor_execute", count)
    try:
        await indexing.drain()
    finally:
        event.remove(indexing.database.engine.sync_engine, "before_cursor_execute", count)
    assert inserts == [320]
    async with indexing.database.session() as session:
        chunk = await session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.document_id == document_id, DocumentChunk.ordinal == 7
            )
        )
        assert chunk is not None
        same = await session.scalar(
            text(
                "SELECT search_tsv = array_to_tsvector(CAST(:lexemes AS text[]))"
                " FROM document_chunks WHERE id = :id"
            ),
            {"lexemes": list(lexical_tokens(chunk.content)), "id": chunk.id},
        )
    assert same is True
