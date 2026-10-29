"""The read-only triage command answers with workspace ids and counts only (FX-0.2 .. FX-0.4)."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models.knowledge import Document
from app.services.knowledge_service import KnowledgeService
from scripts.rag_triage import triage
from tests.integration.rag_harness import POLICY, Indexing

pytestmark = pytest.mark.integration


async def test_triage_finds_each_problem_and_prints_no_text(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    clean = await indexing.workspace()
    await indexing.submit(clean, "Clean\n\nNothing wrong here at all, one copy only.")
    first = await indexing.submit(tenant_id)
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        sales = await knowledge.create_knowledge_base(name="Sales")
        await knowledge.submit(knowledge_base_id=sales.id, title="Doc", raw=POLICY)
    await indexing.drain()
    # What a document stored before this remediation looks like.
    await indexing.execute(
        update(Document)
        .where(Document.id == first)
        .values(
            content="\ufef3\ufee4\ufedc\ufee6", extraction_warnings={"digits_glued_to_arabic": 1}
        )
    )

    engine = create_async_engine(indexing.url)
    try:
        async with engine.connect() as connection:
            report = await triage(connection)
    finally:
        await engine.dispose()

    mine = {name: dict(rows).get(str(tenant_id)) for name, rows in report.items()}
    assert mine == {
        "duplicate-titles": 1,
        "cross-kb-copies": 1,
        "presentation-forms": 1,
        "extraction-warnings": 1,
        "unreachable-knowledge": 2,
    }
    assert all(
        str(clean) not in dict(rows)
        for name, rows in report.items()
        if name != "unreachable-knowledge"
    )
    for rows in report.values():
        for tenant, count in rows:
            assert isinstance(count, int) and len(tenant) == 36
