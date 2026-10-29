"""Every document can be found and managed (RV-13, FX-2.4, P-R10)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ConflictError
from app.db.models.knowledge import Document, DocumentSource, DocumentStatus
from app.services.knowledge_service import KnowledgeService, content_hash
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration


async def _many(
    indexing: Indexing, tenant_id: uuid.UUID, count: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """`count` documents, inserted directly: what is under test is the listing."""
    async with indexing.database.session() as session:
        base = await KnowledgeService(
            session=session, tenant_id=tenant_id
        ).ensure_default_knowledge_base()
        start = datetime.now(UTC) - timedelta(days=1)
        rows = []
        for n in range(count):
            text = f"Document number {n}."
            rows.append(
                Document(
                    tenant_id=tenant_id,
                    knowledge_base_id=base.id,
                    title=f"Price list {n:03d}" if n % 2 else f"Policy {n:03d}",
                    source=DocumentSource.PDF if n % 3 == 0 else DocumentSource.TEXT,
                    status=DocumentStatus.READY if n % 5 else DocumentStatus.FAILED,
                    content_hash=content_hash(text),
                    content=text,
                    # Ties on created_at on purpose: the id breaks them.
                    created_at=start + timedelta(seconds=n // 3),
                )
            )
        session.add_all(rows)
        await session.flush()
        return base.id, [row.id for row in rows]


async def _all_pages(
    indexing: Indexing, tenant_id: uuid.UUID, **filters: object
) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        async with indexing.database.session() as session:
            views, cursor = await KnowledgeService(
                session=session, tenant_id=tenant_id
            ).list_documents_page(
                limit=100, cursor=cursor, **filters  # type: ignore[arg-type]
            )
        seen += [view.document.id for view in views]
        if cursor is None:
            return seen


async def test_250_documents_are_all_listed_by_cursor_once_each(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    base_id, ids = await _many(indexing, tenant_id, 250)
    listed = await _all_pages(indexing, tenant_id, knowledge_base_id=base_id)
    assert len(listed) == 250 and set(listed) == set(ids)
    # Newest first: the oldest - the ones the 100-cap hid - come last.
    assert listed[-1] in ids[:3]


async def test_filters_narrow_the_listing(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    await _many(indexing, tenant_id, 60)
    priced = await _all_pages(indexing, tenant_id, title="price list")
    assert len(priced) == 30
    failed = await _all_pages(indexing, tenant_id, status=DocumentStatus.FAILED)
    assert len(failed) == 12
    pdfs = await _all_pages(indexing, tenant_id, source=DocumentSource.PDF)
    assert len(pdfs) == 20
    assert await _all_pages(indexing, tenant_id, title="100%_") == []


async def test_similar_documents_are_the_likely_older_versions(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        current, _ = await knowledge.submit(
            knowledge_base_id=base.id,
            title="Price list",
            raw="Version two.",
            filename="prices-v2.pdf",
        )
        old, _ = await knowledge.submit(
            knowledge_base_id=base.id,
            title="PRICE LIST",
            raw="Version one.",
            filename="prices-v1.pdf",
        )
        await knowledge.submit(
            knowledge_base_id=base.id, title="Opening hours", raw="Nine to five."
        )
        similar = await knowledge.similar_documents(current.document.id)
    assert [view.document.id for view in similar] == [old.document.id]


async def test_a_knowledge_base_is_renamed_and_deleted_only_when_asked(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        sales = await knowledge.create_knowledge_base(name="Sales")
        await knowledge.submit(knowledge_base_id=sales.id, title="Brochure", raw="Our catalogue.")
        renamed = await knowledge.update_knowledge_base(sales.id, name="Sales 2026")
        assert renamed.name == "Sales 2026"
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        with pytest.raises(ConflictError) as refused:
            await knowledge.delete_knowledge_base(sales.id)
        assert refused.value.error_code == "knowledge_base_not_empty"
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        await knowledge.delete_knowledge_base(sales.id, cascade=True)
        bases, _ = await knowledge.list_knowledge_bases_page(limit=10)
    assert sales.id not in {base.id for base in bases}


async def test_knowledge_bases_page_by_cursor(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id)
        for n in range(7):
            await knowledge.create_knowledge_base(name=f"Base {n}")
    seen: list[uuid.UUID] = []
    cursor = None
    while True:
        async with indexing.database.session() as session:
            bases, cursor = await KnowledgeService(
                session=session, tenant_id=tenant_id
            ).list_knowledge_bases_page(limit=3, cursor=cursor)
        seen += [base.id for base in bases]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7
