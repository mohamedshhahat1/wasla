"""Arabic PDFs are stored as the letters a customer types (RV-01, RV-04, RV-23, P-R03).

Every PDF goes through the real bounded extractor - a child process, asserted
to have run - and what is asserted is `documents.content` read back from
PostgreSQL and the passages `RetrievalService.search` returns for a normally
typed query, never a return value in between.
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models.knowledge import Document, DocumentSource
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_policy import resolve
from app.services.retrieval_ranking import VECTOR
from app.services.retrieval_service import RetrievalService
from app.services.text_normalisation import for_matching
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "pdf"
PRODUCERS = ("chromium", "word", "weasyprint")
PRESENTATION_FORMS = re.compile("[\ufb50-\ufdff\ufe70-\ufeff]")
# "Can the customer return the product within fourteen days", as typed.
TYPED = (
    "\u0647\u0644 \u064a\u0645\u0643\u0646 \u0644\u0644\u0639\u0645\u064a\u0644 "
    "\u0627\u0633\u062a\u0631\u062c\u0627\u0639 \u0627\u0644\u0645\u0646\u062a\u062c "
    "\u062e\u0644\u0627\u0644 \u0623\u0631\u0628\u0639\u0629 \u0639\u0634\u0631 \u064a\u0648\u0645\u0627"  # noqa: E501
)
PHRASE = (
    "\u064a\u0645\u0643\u0646 \u0644\u0644\u0639\u0645\u064a\u0644 "
    "\u0627\u0633\u062a\u0631\u062c\u0627\u0639 \u0627\u0644\u0645\u0646\u062a\u062c"
)


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every extraction child started, so a test proves the real parser ran."""
    started: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return started


async def _submit_pdf(indexing: Indexing, tenant_id: Any, path: Path) -> Document:
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(
            knowledge_base_id=base.id,
            title=path.stem,
            raw=base64.b64encode(path.read_bytes()).decode("ascii"),
            source=DocumentSource.PDF,
            filename=path.name,
        )
        document_id = view.document.id
    async with indexing.database.session() as session:
        stored = await session.scalar(select(Document).where(Document.id == document_id))
        assert stored is not None
        return stored


@pytest.mark.parametrize("producer", PRODUCERS)
async def test_an_arabic_pdf_is_stored_as_letters_and_found_by_a_typed_query(
    indexing: Indexing, children: list[Any], producer: str
) -> None:
    tenant_id = await indexing.workspace()
    stored = await _submit_pdf(indexing, tenant_id, PDFS / f"{producer}-AR-1.pdf")
    assert children and str(children[0][1]).endswith("pdf_extract_child.py")
    assert stored.content is not None
    assert not PRESENTATION_FORMS.search(stored.content)
    assert for_matching(PHRASE) in for_matching(re.sub(r"\s+", " ", stored.content))

    await indexing.drain()
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=TYPED, top_k=4)
    assert any(
        for_matching(PHRASE) in for_matching(re.sub(r"\s+", " ", p.content)) for p in found.passages
    )


async def test_the_chromium_pdf_flags_its_reordered_price_line(
    indexing: Indexing, children: list[Any]
) -> None:
    tenant_id = await indexing.workspace()
    stored = await _submit_pdf(indexing, tenant_id, PDFS / "chromium-AR-1.pdf")
    assert stored.extraction_warnings == {"digits_glued_to_arabic": 1}
    stored = await _submit_pdf(indexing, tenant_id, PDFS / "word-AR-2.pdf")
    # Word's text layer reverses the Arabic-Indic price lines.
    assert stored.extraction_warnings == {"arabic_indic_leading_zero": 2}


async def test_page_furniture_is_not_indexed(indexing: Indexing, children: list[Any]) -> None:
    tenant_id = await indexing.workspace()
    for producer in ("chromium", "weasyprint"):
        stored = await _submit_pdf(indexing, tenant_id, PDFS / f"{producer}-MX-1.pdf")
        assert stored.content is not None
        assert "Service Handbook" not in stored.content
        assert "Confidential" not in stored.content
        assert not [line for line in stored.content.split("\n") if line.strip().isdigit()]
        assert re.search(r"the deposit\s+and the remaining", stored.content)


async def test_a_query_typed_in_arabic_indic_digits_finds_ascii_digits(indexing: Indexing) -> None:
    """End to end with the default policy. Hybrid is on there, and the exact-word
    channel can find the chunk on its own; the vector half is pinned by
    `test_the_vector_search_reads_the_folded_query_with_hybrid_off` (RGM-08)."""
    tenant_id = await indexing.workspace()
    await indexing.submit(
        tenant_id,
        "\u0633\u0639\u0631 \u0627\u0644\u062a\u0634\u0637\u064a\u0628 4500 \u062c\u0646\u064a\u0647\n\n"  # noqa: E501
        "Opening hours are nine to five every day except Friday.",
    )
    await indexing.drain()
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query="\u0633\u0640\u0639\u0631 \u0664\u0665\u0660\u0660", top_k=1)
    assert found.passages and "4500" in found.passages[0].content


async def test_the_vector_search_reads_the_folded_query_with_hybrid_off(
    indexing: Indexing,
) -> None:
    """The query is folded for matching before it is embedded, not only for the
    exact-word channel (RGM-08): with that channel off, a question typed with
    tatweel and Arabic-Indic digits still finds the passage stored with plain
    letters and ASCII digits - by the vector search alone."""
    indexing.configure(rag_hybrid_enabled=False)
    switches = resolve(
        indexing.settings, None, embedding_model=indexing.settings.openai_embedding_model
    )
    assert (
        switches.policy.hybrid is False
    ), "hybrid is off: this test is about the vector query, not the exact-word channel"
    tenant_id = await indexing.workspace()
    await indexing.submit(
        tenant_id,
        "سعر التشطيب 4500 جنيه\n\n" "Opening hours are nine to five every day except Friday.",
    )
    await indexing.drain()
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session,
            tenant_id=tenant_id,
            embeddings=as_embeddings(FakeEmbeddings()),
            policy=switches.policy,
        ).search(query="سـعر ٤٥٠٠", top_k=1)
    assert found.passages, "the folded query found nothing by vector search"
    (passage,) = found.passages
    assert "4500" in passage.content
    assert passage.channels == (VECTOR,), passage.channels


async def test_the_vector_search_reads_the_folded_alternate_query_with_hybrid_off(
    indexing: Indexing,
) -> None:
    """The alternate query is folded like the query (RGM-15): asked in words the
    document does not hold, with the price question as the alternate - typed
    with tatweel and Arabic-Indic digits - the vector search alone finds it."""
    indexing.configure(rag_hybrid_enabled=False)
    switches = resolve(
        indexing.settings, None, embedding_model=indexing.settings.openai_embedding_model
    )
    assert (
        switches.policy.hybrid is False
    ), "hybrid is off: this test is about the vector query, not the exact-word channel"
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id, "سعر التشطيب 4500 جنيه")
    await indexing.drain()
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session,
            tenant_id=tenant_id,
            embeddings=as_embeddings(FakeEmbeddings()),
            policy=switches.policy,
        ).search(query="warranty claims", alternate_query="سـعر ٤٥٠٠", top_k=1)
    assert found.passages, "the folded alternate query found nothing by vector search"
    (passage,) = found.passages
    assert "4500" in passage.content
    assert passage.channels == (VECTOR,), passage.channels
