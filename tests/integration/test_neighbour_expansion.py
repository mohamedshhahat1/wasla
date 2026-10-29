"""Answers spanning two chunks arrive whole; a re-rank never costs a search (FX-3.11, P-R25).

Real repository search on PostgreSQL; the embedding model is the suite's fake,
and the re-rank model a scripted provider at the HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.agents.memory import estimate_tokens
from app.db.models.knowledge import DocumentChunk
from app.integrations.openai.client import ResponsesClient
from app.services.knowledge_rerank import LlmReranker
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_policy import RetrievalPolicy
from app.services.retrieval_service import MAX_CONTEXT_TOKENS, RetrievalService
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.ai_harness import text_response
from tests.integration.isolation_gate import build_twins
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

TRIMS = ("oak", "walnut", "teak", "ash")
ROWS = "\n".join(
    f"Row {n}: package BK-{n} with {TRIMS[n % 4]} trim costs {7000 + n * 50} EGP" for n in range(36)
)
TABLE = f"# Packages\n\n## Premium packages\n\n{ROWS}\n"
OTHER = "# Other\n\n## Premium packages\n\n" + "\n".join(
    f"Other document line {n} about a different supplier entirely" for n in range(36)
)


async def _documents(indexing: Indexing, *bodies: tuple[str, str]) -> uuid.UUID:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        for title, body in bodies:
            await knowledge.submit(
                knowledge_base_id=base.id, title=title, raw=body, filename=f"{title}.md"
            )
    await indexing.drain()
    return tenant_id


async def _chunks(indexing: Indexing, tenant_id: uuid.UUID, title_text: str) -> list[DocumentChunk]:
    async with indexing.database.session() as session:
        rows = list(
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.tenant_id == tenant_id)
                .order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
            )
        )
    return [row for row in rows if title_text in row.content]


async def test_the_chunk_after_the_best_one_arrives_with_it(indexing: Indexing) -> None:
    tenant_id = await _documents(indexing, ("Packages", TABLE), ("Other", OTHER))
    chunks = await _chunks(indexing, tenant_id, "package BK-")
    # Non-vacuity: the table really is several chunks of one section.
    assert len(chunks) >= 2 and chunks[0].heading_path == chunks[1].heading_path
    last_of_first = chunks[0].content.splitlines()[-1]
    last_of_second = chunks[1].content.splitlines()[-1]
    query = last_of_first.split(":", 1)[1]

    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=query, top_k=1, max_distance=2.0)

    (passage,) = found.passages
    assert last_of_first in passage.content
    assert last_of_second in passage.content
    assert passage.ordinals[:2] == (chunks[0].ordinal, chunks[1].ordinal)
    # Never another document's chunk at the same ordinal (M-R34).
    assert "Other document" not in passage.content
    assert estimate_tokens(found.as_context()) <= MAX_CONTEXT_TOKENS


async def test_adjacent_chunks_found_together_are_one_passage_without_their_overlap(
    indexing: Indexing,
) -> None:
    tenant_id = await _documents(indexing, ("Packages", TABLE))
    chunks = await _chunks(indexing, tenant_id, "package BK-")
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query="package trim costs EGP Row", top_k=3, max_distance=2.0)
    merged = [p for p in found.passages if len(p.ordinals) > 1]
    assert merged
    for passage in merged:
        lines = [line for line in passage.content.splitlines() if line.strip()]
        # Each row once: the overlap the chunker repeated is not repeated here.
        assert len(lines) == len(set(lines))
    assert {o for p in found.passages for o in p.ordinals} <= {c.ordinal for c in chunks}


def _reranker(handler: Any) -> LlmReranker:
    async def handle(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = await handler(json.loads(request.content))
        return response

    return LlmReranker(
        client=ResponsesClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            api_key="sk-test-not-real",
        ),
        model="gpt-4.1-nano",
        timeout_seconds=0.3,
    )


async def _ranked(
    indexing: Indexing, tenant_id: uuid.UUID, reranker: LlmReranker | None
) -> list[str]:
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session,
            tenant_id=tenant_id,
            embeddings=as_embeddings(FakeEmbeddings()),
            policy=RetrievalPolicy(rerank=reranker is not None),
            reranker=reranker,
        ).search(query="shipping delivery days Cairo Alexandria Aswan", top_k=3, max_distance=2.0)
    return [p.document_title for p in found.passages]


async def test_the_re_ranked_order_is_what_the_model_receives(indexing: Indexing) -> None:
    tenant_id = await _documents(
        indexing,
        ("Cairo", "Shipping delivery to Cairo takes one day."),
        ("Alexandria", "Shipping delivery to Alexandria takes two days."),
        ("Aswan", "Shipping to Aswan takes five days by road freight."),
    )
    fused = await _ranked(indexing, tenant_id, None)

    async def prefer_the_last(body: dict[str, Any]) -> httpx.Response:
        count = len(json.loads(body["input"][0]["content"])["passages"])
        scores = [{"source": n, "score": 3 if n == count else 0} for n in range(1, count + 1)]
        return httpx.Response(200, json=text_response(json.dumps({"scores": scores})))

    reranked = await _ranked(indexing, tenant_id, _reranker(prefer_the_last))

    assert reranked[0] == fused[-1]
    assert sorted(reranked) == sorted(fused)


async def test_a_re_rank_that_times_out_leaves_the_fused_order(indexing: Indexing) -> None:
    """M-R37: the model receives the fused order when the judge is too slow."""
    tenant_id = await _documents(
        indexing,
        ("Cairo", "Shipping delivery to Cairo takes one day."),
        ("Alexandria", "Shipping delivery to Alexandria takes two days."),
        ("Aswan", "Shipping to Aswan takes five days by road freight."),
    )
    fused = await _ranked(indexing, tenant_id, None)

    async def slow(_body: dict[str, Any]) -> httpx.Response:
        await asyncio.sleep(3)
        return httpx.Response(200, json=text_response("{}"))

    assert await _ranked(indexing, tenant_id, _reranker(slow)) == fused


async def test_re_ranked_and_expanded_results_return_nothing_foreign(indexing: Indexing) -> None:
    """Isolation gate: the re-ranked and the neighbour-expanded paths (IMP-23)."""
    twins = await build_twins(indexing)

    async def keep(body: dict[str, Any]) -> httpx.Response:
        count = len(json.loads(body["input"][0]["content"])["passages"])
        scores = [{"source": n, "score": 1} for n in range(1, count + 1)]
        return httpx.Response(200, json=text_response(json.dumps({"scores": scores})))

    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session,
            tenant_id=twins.viewer,
            embeddings=as_embeddings(FakeEmbeddings()),
            policy=RetrievalPolicy(rerank=True),
            reranker=_reranker(keep),
        ).search(query="7900 EGP premium finishing refund", top_k=6, max_distance=2.0)
    assert found.passages
    twins.assert_isolated(found.passages)
    twins.assert_isolated(found.as_context())
