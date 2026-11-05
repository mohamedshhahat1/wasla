"""Every knowledge invariant can fail (RR-01 .. RR-14, section 27 of the Part 1 prompt).

A count that is zero because its query cannot see the rows it is about proves
nothing. Each check here starts from real indexed documents, injects exactly
one violation inside a transaction that is rolled back - dropping the index or
disabling the foreign-key triggers that would otherwise refuse it - and asserts
that its own count rises.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from scripts.rag_invariants import missing_originals, rag_violations
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

Inject = Callable[[AsyncConnection, dict[str, uuid.UUID]], Awaitable[None]]


async def _sql(connection: AsyncConnection, statement: str, **values: object) -> None:
    await connection.execute(text(statement), values)


async def _two_ready_documents(indexing: Indexing) -> dict[str, uuid.UUID]:
    tenant_id = await indexing.workspace()
    first = await indexing.submit(tenant_id, "Branches\n\nThe branch opens at nine.")
    second = await indexing.submit(tenant_id, "Refunds\n\nRefunds take fourteen days.")
    await indexing.drain()
    await indexing.reindex(tenant_id, first)
    await indexing.drain()
    unindexed = await indexing.submit(tenant_id, "Pending\n\nNot indexed yet.", enqueue=False)
    return {"tenant": tenant_id, "first": first, "second": second, "unindexed": unindexed}


@pytest.fixture
async def connection(indexing: Indexing) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(indexing.url)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def _rr01(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(c, "DROP INDEX uq_document_index_generations_active")
    await _sql(
        c,
        "UPDATE document_index_generations SET state = 'active'"
        " WHERE document_id = :d AND state = 'superseded'",
        d=ids["first"],
    )


async def _rr02(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(c, "UPDATE documents SET status = 'ready' WHERE id = :d", d=ids["unindexed"])


async def _rr03(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(
        c,
        "UPDATE document_index_generations SET state = 'superseded'"
        " WHERE document_id = :d AND state = 'active'",
        d=ids["second"],
    )


async def _rr04(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    # The same text in a second knowledge base of the workspace (pre-RQ-08).
    await _sql(
        c,
        "ALTER TABLE documents"
        " DROP CONSTRAINT uq_documents_tenant_id_knowledge_base_id_content_hash",
    )
    await _sql(
        c,
        "UPDATE documents SET content_hash = (SELECT content_hash FROM documents WHERE id = :a)"
        " WHERE id = :b",
        a=ids["first"],
        b=ids["second"],
    )


async def _rr05(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(c, "DROP INDEX uq_documents_tenant_kb_source_key")
    await _sql(
        c,
        "UPDATE documents SET source_key = 'pricing.pdf' WHERE id IN (:a, :b)",
        a=ids["first"],
        b=ids["second"],
    )


async def _rr06(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(
        c,
        "UPDATE document_chunks SET content = :glyphs WHERE document_id = :d",
        glyphs="ﻳﻤﻜﻦ",
        d=ids["second"],
    )


async def _rr07_hash(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(
        c, "UPDATE documents SET content_hash = repeat('0', 64) WHERE id = :d", d=ids["first"]
    )


async def _rr07_text(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    # Hashed correctly, but holding a zero-width space `for_storage` removes.
    await _sql(
        c,
        "UPDATE documents SET content = :t,"
        " content_hash = encode(sha256(convert_to(:t, 'UTF8')), 'hex') WHERE id = :d",
        t="Branches​\n\nThe branch opens at nine.",
        d=ids["first"],
    )


async def _rr08(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(c, "SET LOCAL session_replication_role = replica")
    await _sql(
        c,
        "UPDATE document_chunks SET tenant_id = gen_random_uuid() WHERE document_id = :d",
        d=ids["second"],
    )


async def _rr12(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    # A chunk published before migration 0132: served, but not by exact words.
    await _sql(
        c,
        "UPDATE document_chunks SET search_text = NULL, search_tsv = NULL WHERE document_id = :d",
        d=ids["second"],
    )


async def _rr09(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    # Served from a space nobody queries in, with no switch under way (RV-11).
    await _sql(
        c,
        "UPDATE document_index_generations SET embedding_model = 'text-embedding-3-large'"
        " WHERE document_id = :d AND state = 'active'",
        d=ids["second"],
    )


async def _rr13(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await _sql(
        c,
        "INSERT INTO knowledge_search_gaps (id, tenant_id, query, language, created_at)"
        " VALUES (gen_random_uuid(), :t, 'old question', 'en', now() - interval '45 days')",
        t=ids["tenant"],
    )


async def _rr14(c: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    # A catalogue chunk packed with two rows (M-R58's shape).
    await _sql(c, "UPDATE documents SET source = 'csv' WHERE id = :d", d=ids["second"])
    await _sql(
        c,
        "UPDATE document_chunks SET content = 'SKU: A; Price: 1' || chr(10) || 'SKU: B; Price: 2'"
        " WHERE document_id = :d",
        d=ids["second"],
    )


CASES: dict[str, Inject] = {
    "rr01_document_with_more_than_one_active_generation": _rr01,
    "rr02_ready_document_not_served_by_one_active_generation_with_chunks": _rr02,
    "rr03_chunk_of_a_generation_not_active": _rr03,
    "rr04_ready_text_served_twice_in_a_workspace": _rr04,
    "rr05_source_key_held_twice_in_a_knowledge_base": _rr05,
    "rr06_stored_text_in_arabic_presentation_forms": _rr06,
    "rr07_content_hash_not_the_hash_of_the_stored_text": _rr07_hash,
    "rr07_stored_text_not_normalised": _rr07_text,
    "rr08_chunk_disagreeing_with_its_parents": _rr08,
    "rr12_active_chunk_without_lexical_text": _rr12,
    "rr09_active_generation_outside_the_query_space": _rr09,
    "rr13_gap_row_past_its_retention": _rr13,
    "rr14_catalogue_chunk_holding_more_than_one_row": _rr14,
}


@pytest.mark.parametrize("name", sorted(CASES))
async def test_each_invariant_counts_the_violation_injected_for_it(
    indexing: Indexing, connection: AsyncConnection, name: str
) -> None:
    ids = await _two_ready_documents(indexing)
    transaction = await connection.begin()
    try:
        before = await rag_violations(connection)
        assert before[name] == 0, "the healthy rows these tests made break nothing"
        await CASES[name](connection, ids)
        after = await rag_violations(connection)
        assert after[name] > before[name]
    finally:
        await transaction.rollback()


class Store:
    """An object store that holds exactly the keys it is given."""

    def __init__(self, held: set[str]) -> None:
        self.held = held

    async def exists(self, key: str) -> bool:
        return key in self.held


async def test_an_original_the_store_lost_is_counted(
    indexing: Indexing, connection: AsyncConnection
) -> None:
    ids = await _two_ready_documents(indexing)
    transaction = await connection.begin()
    try:
        await _sql(
            connection,
            "UPDATE document_index_generations SET original_object_key = :k"
            " WHERE document_id = :d AND state = 'active'",
            k=f"{ids['tenant']}/originals/rr10-probe",
            d=ids["second"],
        )
        key = f"{ids['tenant']}/originals/rr10-probe"
        name = "rr10_original_named_but_not_stored"
        held = await missing_originals(connection, Store({key}))  # type: ignore[arg-type]
        lost = await missing_originals(connection, Store(set()))  # type: ignore[arg-type]
        # Whatever other rows name, the only difference is the one key.
        assert lost[name] - held[name] == 1
    finally:
        await transaction.rollback()
