"""Repairing knowledge stored before the remediation (RAG_SYSTEM_REVIEW.md §10 warning).

The back-fill and `reindex-all` on real PostgreSQL and Redis, through the real
worker. Pre-remediation documents are made the only way they can exist now: a
document written by the current code, then its stored text rewritten to what
the old code stored.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
from sqlalchemy import select, update

from app.db.models.knowledge import Document, DocumentIndexGeneration, GenerationState
from app.services.knowledge_service import KnowledgeService, content_hash
from app.services.text_normalisation import for_storage
from app.workers.knowledge_repair import backfill_normalised_content, reindex_all
from tests.integration.rag_harness import Indexing, respond

pytestmark = pytest.mark.integration

GLYPHS = "\ufef3\ufee4\ufedc\ufee6 \ufedf\ufee0\ufecc\ufee4\ufef4\ufede"  # "can the customer"
PRESENTATION_FORMS = re.compile("[\ufb50-\ufdff\ufe70-\ufeff]")


async def _as_stored_before(indexing: Indexing, document_id: uuid.UUID, content: str) -> None:
    """Rewrite a document to what the pre-remediation code stored: raw text, raw hash."""
    await indexing.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(content=content, content_hash=content_hash(content), extraction_warnings=None)
    )


async def test_the_backfill_normalises_hashes_and_reports_collisions(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    glyph = await indexing.submit(tenant_id, "placeholder one\n\nwill be rewritten")
    await _as_stored_before(indexing, glyph, f"Refund policy\n\n{GLYPHS} 14 days")
    # A newer upload of the same words, already normalised: the collision.
    older = await indexing.submit(tenant_id, "placeholder two\n\nwill be rewritten")
    await _as_stored_before(indexing, older, f"Opening hours\n\n{GLYPHS} nine to five")
    newer = await indexing.submit(tenant_id, for_storage(f"Opening hours\n\n{GLYPHS} nine to five"))
    clean = await indexing.submit(tenant_id, "Nothing to change here at all.")

    dry = await backfill_normalised_content(indexing.database, dry_run=True)
    assert dry.updated == 1 and dry.collisions == [(older, newer)]
    assert (await indexing.document(glyph)).content.startswith("Refund policy\n\n\ufef3")  # type: ignore[union-attr]

    report = await backfill_normalised_content(indexing.database)
    assert report.updated == 1
    assert report.collisions == [(older, newer)]
    for document_id in (glyph, newer, clean):
        stored = await indexing.document(document_id)
        assert stored is not None and stored.content is not None
        assert not PRESENTATION_FORMS.search(stored.content)
        # RR-07: the stored hash is the hash of the stored text.
        assert stored.content_hash == content_hash(for_storage(stored.content))
    # The collision is left for the operator: nothing merged, nothing deleted.
    kept = await indexing.document(older)
    assert kept is not None and PRESENTATION_FORMS.search(kept.content or "")

    again = await backfill_normalised_content(indexing.database)
    assert again.updated == 0 and again.collisions == [(older, newer)]


async def test_a_pdf_whose_stored_lines_look_reordered_is_listed_for_reupload(
    indexing: Indexing,
) -> None:
    from app.db.models.knowledge import DocumentSource

    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, "placeholder\n\nrewritten below")
    scrambled = " \u062c\u0646\u064a\u0647 \u0644\u0644\u0645\u062a\u0631.4500\u0633\u0639\u0631"
    await indexing.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(source=DocumentSource.PDF, content=scrambled, extraction_warnings=None)
    )
    report = await backfill_normalised_content(indexing.database)
    assert report.reupload == [document_id]
    stored = await indexing.document(document_id)
    assert stored is not None and stored.extraction_warnings == {"digits_glued_to_arabic": 1}


async def test_reindex_all_keeps_every_document_served_and_resumes(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    ids = [
        await indexing.submit(tenant_id, f"Branch {n}\n\nThe branch number {n} opens at nine.")
        for n in range(5)
    ]
    await indexing.drain()
    indexing.provider.handler = respond(404)
    broken = await indexing.submit(tenant_id, "Broken\n\nThis one fails permanently.")
    await indexing.drain()
    indexing.provider.handler = None

    async def search() -> set[str]:
        return {content for _, content in await indexing.search(tenant_id, "branch opens nine")}

    before = await search()
    assert len(before) == 5

    paused: list[float] = []

    async def pause(seconds: float) -> None:
        paused.append(seconds)
        # Old generations keep serving while the new ones are only requested.
        assert await search() == before

    first = await reindex_all(indexing.database, indexing.queue(), rate=10, limit=3, pause=pause)
    # Pages run in document-id order, so the failed document lands in either
    # page; it counts towards the page it is in and is never re-indexed.
    assert first.requested + len(first.skipped_failed) == 3
    assert paused == [0.1] * first.requested
    second = await reindex_all(indexing.database, indexing.queue(), rate=0, after=first.next_after)
    assert second.next_after is None
    assert first.skipped_failed + second.skipped_failed == [broken]
    assert first.requested + second.requested == 5

    indexing.provider.gate = asyncio.Event()
    indexing.provider.entered = asyncio.Event()
    drain = asyncio.create_task(indexing.drain(budget=20))
    await asyncio.wait_for(indexing.provider.entered.wait(), 10)
    assert await search() == before  # served while the rebuild is held at the provider
    indexing.provider.gate.set()
    await drain

    assert await search() == before
    async with indexing.database.session() as session:
        for document_id in ids:
            active = list(
                await session.scalars(
                    select(DocumentIndexGeneration).where(
                        DocumentIndexGeneration.document_id == document_id,
                        DocumentIndexGeneration.state == GenerationState.ACTIVE,
                    )
                )
            )
            assert len(active) == 1 and active[0].number == 2


async def test_reindex_all_rewrites_stored_glyph_documents_into_normalised_chunks(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, "placeholder\n\nrewritten")
    await indexing.drain()
    await _as_stored_before(indexing, document_id, f"Refund policy\n\n{GLYPHS}")
    await backfill_normalised_content(indexing.database)
    async with indexing.database.session() as session:
        await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).reindex(document_id)
    await indexing.drain()
    found = await indexing.search(
        tenant_id, "\u064a\u0645\u0643\u0646 \u0644\u0644\u0639\u0645\u064a\u0644"
    )
    assert found and not PRESENTATION_FORMS.search(found[0][1])
