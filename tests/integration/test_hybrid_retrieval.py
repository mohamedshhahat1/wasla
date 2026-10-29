"""The exact-word channel beside the vector one (RC-4, RQ-15, FX-3.3, P-R22).

The workspaces here are indexed and queried by `IdentifierBlindEmbeddings`: the
suite's bag of words with every token holding a digit removed, which is how an
embedding model treats an identifier it has never seen. So a passage found here
for a SKU was found by its exact words - each test first proves the vector
channel alone does not return it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db.models.knowledge import DocumentChunk, DocumentIndexGeneration, GenerationState
from app.repositories.knowledge_repository import DocumentChunkRepository
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_policy import RetrievalPolicy
from app.services.retrieval_ranking import LEXICAL, lexical_tokens, tsquery_text
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import embed_text
from tests.integration.rag_eval_part2 import IdentifierBlindEmbeddings, blind_provider
from tests.integration.rag_harness import SMALL, Indexing

pytestmark = pytest.mark.integration

CATALOGUE = (
    "BK-35 premium sofa, delivered in three weeks, upholstery linen, legs walnut, "
    "cushions feather."
)
DISTRACTORS = (
    "Sofa price list: the classic sofa price is 4,500 EGP and the corner sofa price is 9,000 EGP.",
    "Price of every bed frame includes delivery. Price of every wardrobe includes assembly.",
    "Laptop catalogue: the Ultrabook Pro sells for 45,000 EGP with a two-year warranty.",
)


async def _workspace(
    indexing: Indexing, documents: list[tuple[str, str]], **kwargs: Any
) -> uuid.UUID:
    indexing.provider.handler = blind_provider
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        for title, body in documents:
            await knowledge.submit(knowledge_base_id=base.id, title=title, raw=body, **kwargs)
    await indexing.drain()
    return tenant_id


async def _search(
    indexing: Indexing, tenant_id: uuid.UUID, query: str, *, hybrid: bool = True
) -> Any:
    async with indexing.database.session() as session:
        return await RetrievalService(
            session=session,
            tenant_id=tenant_id,
            embeddings=IdentifierBlindEmbeddings(),  # type: ignore[arg-type]
            policy=RetrievalPolicy(hybrid=hybrid),
        ).search(query=query, top_k=4)


async def test_a_sku_the_embedding_misses_is_found_by_its_exact_words(indexing: Indexing) -> None:
    tenant_id = await _workspace(
        indexing,
        [("Catalogue", CATALOGUE), *[(f"Prices {n}", body) for n, body in enumerate(DISTRACTORS)]],
    )

    vector_only = await _search(indexing, tenant_id, "BK-35 price", hybrid=False)
    hybrid = await _search(indexing, tenant_id, "BK-35 price")

    # Non-vacuity: the vector channel alone does not return it.
    assert not any("BK-35" in p.content for p in vector_only.passages)
    (found,) = [p for p in hybrid.passages if "BK-35" in p.content]
    assert LEXICAL in found.channels
    # Kept by the identifier, not by distance: it is farther than the cut-off.
    assert found.distance > 0.75


async def test_a_latin_brand_inside_an_arabic_question_is_found(indexing: Indexing) -> None:
    tenant_id = await _workspace(
        indexing, [(f"Prices {n}", body) for n, body in enumerate(DISTRACTORS)]
    )
    query = "عايز أعرف سعر Ultrabook كام"

    vector_only = await _search(indexing, tenant_id, query, hybrid=False)
    hybrid = await _search(indexing, tenant_id, query)

    assert not any("Ultrabook" in p.content for p in vector_only.passages)
    assert any("Ultrabook" in p.content and LEXICAL in p.channels for p in hybrid.passages)


async def test_a_far_lexical_hit_on_a_common_word_is_dropped(indexing: Indexing) -> None:
    """M-R48: the keep rule admits identifiers, not every word the passage shares."""
    tenant_id = await _workspace(indexing, [("Catalogue", CATALOGUE)])
    query = "walnut colour options for the dining chairs and matching tables"

    async with indexing.database.session() as session:
        lexical = await DocumentChunkRepository(session, tenant_id=tenant_id).lexical_search(
            tsquery=tsquery_text(lexical_tokens(query)) or "",
            embedding=embed_text(query),
            space=SMALL,
        )
    hybrid = await _search(indexing, tenant_id, query)

    # Non-vacuity: the exact-word channel did find it, on "walnut".
    assert lexical and lexical[0].distance > 0.75
    assert hybrid.passages == ()


async def test_an_expired_document_is_never_retrieved_by_its_exact_words(
    indexing: Indexing,
) -> None:
    """M-R46 / M-R36: the validity window holds on the lexical channel too."""
    now = datetime.now(UTC)
    tenant_id = await _workspace(
        indexing, [("Catalogue", CATALOGUE)], valid_until=now - timedelta(days=1)
    )
    async with indexing.database.session() as session:
        # Non-vacuity: the chunk is there, indexed for exact words.
        (chunk,) = await session.scalars(
            select(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id)
        )
        assert chunk.search_tsv is not None
        rows = await DocumentChunkRepository(session, tenant_id=tenant_id).lexical_search(
            tsquery=tsquery_text(["bk-35"]) or "", embedding=embed_text("bk"), space=SMALL
        )
    assert rows == []
    assert (await _search(indexing, tenant_id, "BK-35 price")).passages == ()


async def test_a_superseded_generations_chunk_is_never_retrieved_by_its_exact_words(
    indexing: Indexing,
) -> None:
    """M-R33: a stale chunk - which the swap deletes, and which RR-03 counts - is not served.

    Seeded directly, because the publish never leaves one behind: the only way to
    prove the active-generation predicate on the lexical query is to put a row
    in front of it that only that predicate excludes.
    """
    tenant_id = await _workspace(indexing, [("Catalogue", CATALOGUE)])
    async with indexing.database.session() as session:
        (chunk,) = await session.scalars(
            select(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id)
        )
        await session.execute(
            update(DocumentIndexGeneration)
            .where(DocumentIndexGeneration.id == chunk.generation_id)
            .values(state=GenerationState.SUPERSEDED)
        )
        await session.commit()
    async with indexing.database.session() as session:
        rows = await DocumentChunkRepository(session, tenant_id=tenant_id).lexical_search(
            tsquery=tsquery_text(["bk-35"]) or "", embedding=embed_text("bk"), space=SMALL
        )
    assert rows == []


async def test_an_updated_document_serves_one_version_through_the_hybrid_search(
    indexing: Indexing,
) -> None:
    """P-R05 through the exact-word channel: 7200 until the swap, 7900 only after."""
    from tests.integration.test_document_replacement import V1, V2

    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(knowledge_base_id=base.id, title="Price list", raw=V1)
    await indexing.drain()

    async def prices(query: str) -> list[str]:
        async with indexing.database.session() as session:
            found = await RetrievalService(
                session=session,
                tenant_id=tenant_id,
                embeddings=IdentifierBlindEmbeddings(),  # type: ignore[arg-type]
            ).search(query=query, top_k=6)
        return [p for p in ("7200", "7900") for x in found.passages if p in x.content]

    async with indexing.database.session() as session:
        await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).replace_content(view.document.id, raw=V2)
    # Built but not yet published: the exact words of v2 find nothing.
    assert await prices("7900 EGP premium") == ["7200"]
    await indexing.drain()
    assert await prices("7200 EGP premium") == ["7900"]
    assert await prices("7900 EGP premium") == ["7900"]


async def test_published_chunks_carry_their_lexical_text(indexing: Indexing) -> None:
    tenant_id = await _workspace(indexing, [("Catalogue", CATALOGUE)])
    async with indexing.database.session() as session:
        (chunk,) = await session.scalars(
            select(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id)
        )
    assert chunk.search_text == chunk.search_text.casefold()
    assert "bk-35" in (chunk.search_text or "")
    assert "'bk-35'" in str(chunk.search_tsv)
