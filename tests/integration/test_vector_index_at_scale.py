"""The approximate index after a mass re-index, and what migration 0137 set (FX-4.2, P-R31, P-R32).

A re-index publishes every chunk of a document again under a new id and deletes
the old ones (the swap, ADR-106). Until autovacuum removes them the HNSW graph
still holds the deleted rows' entries, and an approximate scan spends its
candidate budget stepping over them. This proves, on the clustered corpus of
`test_vector_index.py`, that after every chunk of the large workspace has been
re-published the approximate answer is still as close as the exact one and
never names a deleted row - before any vacuum has run.

Vacuum itself cannot run inside the test's rolled-back transaction, so the
before/after-vacuum numbers, and search p50 / p95 per workspace size on a
22,800-chunk corpus, are a local drill (`audit/rag/p_r32_recall.py`,
`audit/rag/p_r31_latency.py`) recorded in `RAG_REMEDIATION_PART3_SCALE.md`
section 8. Runs in the ordinary integration lane: the corpus is the 1,000-chunk
one, seeded in about a second.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.knowledge_repository import DocumentChunkRepository
from tests.integration.test_vector_index import (
    TOP_K,
    _only_an_exact_scan,
    _only_the_ann_index,
    _plan_uses_the_ann_index,
)
from tests.integration.vector_corpus import RECALL_QUERIES, seed_corpus
from tests.knowledge_seed import FAKE_SPACE

pytestmark = pytest.mark.integration


async def _republish_every_chunk(session: AsyncSession, tenant_id: object) -> set[object]:
    """Every chunk of the workspace deleted and inserted again under a new id; the old ids."""
    old = {
        row[0]
        for row in await session.execute(
            text("SELECT id FROM document_chunks WHERE tenant_id = :t"), {"t": tenant_id}
        )
    }
    columns = [
        row[0]
        for row in await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'document_chunks' AND column_name <> 'id'"
                " ORDER BY ordinal_position"
            )
        )
    ]
    names = ", ".join(columns)
    await session.execute(
        text(
            f"CREATE TEMP TABLE republished ON COMMIT DROP AS"  # noqa: S608 - catalogue names
            f" SELECT {names} FROM document_chunks WHERE tenant_id = :t"
        ),
        {"t": tenant_id},
    )
    await session.execute(
        text("DELETE FROM document_chunks WHERE tenant_id = :t"), {"t": tenant_id}
    )
    await session.execute(
        text(
            f"INSERT INTO document_chunks (id, {names})"  # noqa: S608 - catalogue names
            f" SELECT gen_random_uuid(), {names} FROM republished"
        )
    )
    return old


async def test_after_a_mass_reindex_the_approximate_answer_is_still_as_close_as_the_exact_one(
    db_session: AsyncSession,
) -> None:
    corpus = await seed_corpus(db_session)
    deleted = await _republish_every_chunk(db_session, corpus.large.id)
    assert len(deleted) > 900
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.large.id)

    # Non-vacuity: the approximate side really is the HNSW index, holding the
    # deleted rows' entries beside the new ones.
    async with _only_the_ann_index(db_session):
        assert await _plan_uses_the_ann_index(db_session, corpus, corpus.large.id)

    hits = truth = 0
    for query in corpus.recall_queries:
        async with _only_an_exact_scan(db_session):
            exact = await chunks.search(space=FAKE_SPACE, embedding=query, limit=TOP_K)
        async with _only_the_ann_index(db_session):
            approximate = await chunks.search(space=FAKE_SPACE, embedding=query, limit=TOP_K)

        assert len(approximate) == len(exact) == TOP_K
        assert not {scored.chunk.id for scored in approximate} & deleted
        assert max(s.distance for s in approximate) <= max(s.distance for s in exact) * 1.02
        hits += len({s.chunk.id for s in exact} & {s.chunk.id for s in approximate})
        truth += len(exact)

    assert truth == RECALL_QUERIES * TOP_K
    assert hits / truth >= 0.75


async def test_chunks_are_vacuumed_soon_after_a_reindex_and_workspaces_are_counted(
    db_session: AsyncSession,
) -> None:
    """Migration 0137's storage parameters and statistics target, as the catalogue holds them.

    A model-built schema has neither - they are operational settings, not part
    of the model - so this asserts them only where Alembic built the schema.
    """
    version = await db_session.scalar(
        text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    )
    if not version:
        pytest.skip("storage parameters come from migration 0137; this schema is model-built")
    options = set(
        await db_session.scalar(
            text("SELECT reloptions FROM pg_class WHERE relname = 'document_chunks'")
        )
        or []
    )
    assert {
        "autovacuum_vacuum_scale_factor=0.02",
        "autovacuum_vacuum_threshold=1000",
        "autovacuum_analyze_scale_factor=0.02",
        "autovacuum_analyze_threshold=1000",
        "autovacuum_vacuum_insert_scale_factor=0.05",
    } <= options
    target = await db_session.scalar(
        text(
            "SELECT attstattarget FROM pg_attribute"
            " WHERE attrelid = 'document_chunks'::regclass AND attname = 'tenant_id'"
        )
    )
    assert target == 1000
