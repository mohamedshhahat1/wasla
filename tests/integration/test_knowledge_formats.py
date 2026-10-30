"""Catalogues and office documents as knowledge (RQ-21, FX-3.17, FX-4.5, P-R28).

Every format through the real path: `KnowledgeService` on a committing session,
the sandboxed child for XLSX, DOCX and HTML, the real ingestion worker draining
a real Redis queue, and `RetrievalService` over the real repository. Embeddings
are faked at the HTTP transport, so what is proved is structure: the right row
exists, whole, alone, current and findable by its words.

Before this stage every one of these uploads was refused - `'csv' is not a valid
DocumentSource` (P-R28, recorded at the base head).
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError, ValidationError
from app.db.models.knowledge import DocumentChunk, DocumentSource, DocumentStatus
from app.services import document_formats
from app.services.document_formats import (
    DocumentLimitError,
    FormatMismatchError,
    extract_bounded,
    extract_csv,
)
from app.services.knowledge_limits import MAX_TABLE_COLUMNS, MAX_TABLE_ROWS
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.rag_harness import Indexing
from tests.knowledge_files import (
    PRICE_ROWS,
    csv_text,
    docx_bytes,
    entity_expansion_docx,
    formula_xlsx,
    html_text,
    xlsx_bytes,
    zip_bomb_xlsx,
)
from tests.pdf_fixtures import _assemble, _stream

pytestmark = pytest.mark.integration

FAQ = [
    ("How long does delivery to Cairo take?", "Delivery to Cairo takes one working day."),
    ("Can I return a sofa?", "Sofas can be returned within fourteen days, unused."),
    ("Is installation included?", "Installation is free for wardrobes and beds."),
]


def _raw(source: DocumentSource, rows: list[tuple[str, str, str]] = PRICE_ROWS) -> str:
    """A price list in `source`'s format, as the JSON route carries it."""
    if source is DocumentSource.CSV:
        return csv_text(rows)
    if source is DocumentSource.XLSX:
        return base64.b64encode(xlsx_bytes(rows=rows)).decode()
    if source is DocumentSource.DOCX:
        table = [["SKU", "Product", "Price"], *[list(row) for row in rows]]
        return base64.b64encode(docx_bytes(title="Price list", table=table)).decode()
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    return (
        "<html><body><h1>Price list</h1><table><tr><th>SKU</th><th>Product</th>"
        f"<th>Price</th></tr>{body}</table></body></html>"
    )


def _faq_raw(source: DocumentSource) -> str:
    if source is DocumentSource.TEXT:
        return "Questions\n\n" + "\n\n".join(f"Q: {q}\nA: {a}" for q, a in FAQ)
    if source is DocumentSource.MARKDOWN:
        return "# Questions\n\n" + "\n\n".join(f"{n}. {q}\n{a}" for n, (q, a) in enumerate(FAQ, 1))
    if source is DocumentSource.DOCX:
        return base64.b64encode(docx_bytes(title="Questions", faq=FAQ)).decode()
    if source is DocumentSource.HTML:
        return html_text(title="Questions", faq=FAQ)
    lines = [f"Q: {q}" if i % 2 == 0 else f"A: {a}" for q, a in FAQ for i in range(2)]
    pairs = []
    for index, line in enumerate(lines):
        pairs.append(
            b"(" + line.encode("latin-1") + b") Tj 0 -16 Td" + (b" 0 -16 Td" if index % 2 else b"")
        )
    content = b"BT /F1 11 Tf 72 740 Td " + b" ".join(pairs) + b" ET"
    pdf = _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>",
            _stream(content, compress=False),
        ]
    )
    return base64.b64encode(pdf).decode()


async def _upload(
    indexing: Indexing,
    tenant_id: uuid.UUID,
    source: DocumentSource,
    raw: str,
    *,
    title: str = "Price list",
    base_name: str | None = None,
    **kwargs: Any,
) -> uuid.UUID:
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = (
            await knowledge.create_knowledge_base(name=base_name)
            if base_name
            else await knowledge.ensure_default_knowledge_base()
        )
        view, _ = await knowledge.submit(
            knowledge_base_id=base.id, title=title, raw=raw, source=source, **kwargs
        )
        return view.document.id


async def _chunks(indexing: Indexing, document_id: uuid.UUID) -> list[DocumentChunk]:
    async with indexing.database.session() as session:
        rows = await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )
        return list(rows)


async def _search(
    indexing: Indexing, tenant_id: uuid.UUID, query: str, top_k: int = 3
) -> list[str]:
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=query, top_k=top_k)
    return [passage.content for passage in found.passages]


NEW_FORMATS = [DocumentSource.CSV, DocumentSource.XLSX, DocumentSource.DOCX, DocumentSource.HTML]


@pytest.mark.parametrize("source", NEW_FORMATS, ids=str)
async def test_a_price_list_in_every_new_format_is_indexed_and_its_row_found(
    indexing: Indexing, source: DocumentSource
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id, source, _raw(source))
    await indexing.drain()
    document = await indexing.document(document_id)
    assert document is not None and document.status is DocumentStatus.READY
    assert document.source is source

    chunks = await _chunks(indexing, document_id)
    if source in (DocumentSource.CSV, DocumentSource.XLSX):
        # One chunk per row (RR-14), the header repeated inside each.
        assert len(chunks) == len(PRICE_ROWS)
        for chunk, (sku, product, price) in zip(chunks, PRICE_ROWS, strict=True):
            assert chunk.content == f"SKU: {sku}; Product: {product}; Price: {price}"
    assert "SCRIPT-TEXT" not in " ".join(c.content for c in chunks)

    found = await _search(indexing, tenant_id, "Bookshelf model 35 price", top_k=1)
    assert found and "Bookshelf model 35" in found[0] and "4,500 EGP" in found[0]
    if source in (DocumentSource.CSV, DocumentSource.XLSX):
        assert found[0].count("SKU:") == 1


@pytest.mark.parametrize("source", NEW_FORMATS, ids=str)
async def test_a_replaced_document_of_every_new_format_serves_only_its_new_version(
    indexing: Indexing, source: DocumentSource
) -> None:
    """P-R05's shape per format: v1, replaced by v2, and only v2 is ever served after."""
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id, source, _raw(source))
    await indexing.drain()
    newer = [
        (sku, product, "4,650 EGP" if sku == "BK-35" else price)
        for sku, product, price in PRICE_ROWS
    ]
    async with indexing.database.session() as session:
        _, outcome = await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).replace_content(document_id, raw=_raw(source, newer), source=source)
    assert outcome == "replacement_started"
    assert any(
        "4,500 EGP" in text for text in await _search(indexing, tenant_id, "Bookshelf model 35")
    )
    await indexing.drain()

    served = " ".join(await _search(indexing, tenant_id, "Bookshelf model 35 price", top_k=6))
    assert "4,650 EGP" in served and "4,500 EGP" not in served
    assert all("4,500" not in chunk.content for chunk in await _chunks(indexing, document_id))


@pytest.mark.parametrize("source", NEW_FORMATS, ids=str)
async def test_the_same_file_in_another_knowledge_base_is_refused(
    indexing: Indexing, source: DocumentSource
) -> None:
    tenant_id = await indexing.workspace()
    raw = _raw(source)
    await _upload(indexing, tenant_id, source, raw)
    with pytest.raises(ConflictError) as refused:
        await _upload(indexing, tenant_id, source, raw, base_name="Sales")
    assert refused.value.error_code == "document_exists_in_other_knowledge_base"


@pytest.mark.parametrize(
    "source",
    [
        DocumentSource.TEXT,
        DocumentSource.MARKDOWN,
        DocumentSource.DOCX,
        DocumentSource.HTML,
        DocumentSource.PDF,
    ],
    ids=str,
)
async def test_each_faq_question_is_one_chunk_with_its_own_answer(
    indexing: Indexing, source: DocumentSource
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id, source, _faq_raw(source), title="Questions")
    await indexing.drain()
    chunks = await _chunks(indexing, document_id)
    for question, answer in FAQ:
        holding = [c.content for c in chunks if question in c.content]
        assert len(holding) == 1, (question, [c.content for c in chunks])
        assert answer in holding[0]
        others = [q for q, _ in FAQ if q != question]
        assert not any(other in holding[0] for other in others)
        found = await _search(indexing, tenant_id, question, top_k=1)
        assert found and answer in found[0]


# --------------------------------------------------------------- hostile files


async def test_a_zip_bomb_workbook_is_refused_inside_the_sandbox() -> None:
    with pytest.raises(DocumentLimitError, match="expands to more than"):
        await extract_bounded(zip_bomb_xlsx(), source=DocumentSource.XLSX)


async def test_a_word_document_declaring_entities_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(DocumentLimitError, match="declares XML entities"):
        await extract_bounded(entity_expansion_docx(), source=DocumentSource.DOCX)


async def test_a_formula_is_stored_as_its_text_and_never_as_a_cached_result() -> None:
    text, warnings = await extract_bounded(formula_xlsx(), source=DocumentSource.XLSX)
    assert "Price: =B2*2" in text
    assert '=HYPERLINK("http://example.test","open")' in text
    assert "9999" not in text
    assert warnings == {"formula_cells": 2}


def test_a_formula_in_a_csv_is_text() -> None:
    text = extract_csv('SKU,Link\nBK-35,"=HYPERLINK(""http://x.test"",""open"")"\n')
    assert text == 'SKU: BK-35; Link: =HYPERLINK("http://x.test","open")'


def test_a_ten_thousand_column_csv_is_refused() -> None:
    wide = ",".join(f"c{n}" for n in range(10_000)) + "\n" + ",".join("v" for _ in range(10_000))
    with pytest.raises(DocumentLimitError, match=f"The limit is {MAX_TABLE_COLUMNS}"):
        extract_csv(wide)


def test_a_utf16_csv_without_a_byte_order_mark_is_read() -> None:
    raw = csv_text(PRICE_ROWS).replace("Bookshelf", "مكتبة").encode("utf-16-le")
    assert not raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    text = extract_csv(document_formats.decode_csv_bytes(raw))
    assert "Product: مكتبة model 35; Price: 4,500 EGP" in text


def test_a_catalogue_is_accepted_up_to_the_row_limit_and_refused_past_it() -> None:
    rows = [(f"SKU-{n}", f"Item {n}", f"{n} EGP") for n in range(MAX_TABLE_ROWS)]
    assert extract_csv(csv_text(rows)).count("\n") == MAX_TABLE_ROWS - 1
    five_thousand = [(f"SKU-{n}", f"Item {n}", f"{n} EGP") for n in range(5_000)]
    with pytest.raises(DocumentLimitError, match=f"more than {MAX_TABLE_ROWS} rows"):
        extract_csv(csv_text(five_thousand))


async def test_a_workbook_past_the_row_limit_is_refused_inside_the_sandbox() -> None:
    rows = [(f"SKU-{n}", f"Item {n}", n) for n in range(MAX_TABLE_ROWS + 1)]
    with pytest.raises(DocumentLimitError, match=f"more than {MAX_TABLE_ROWS} rows"):
        await extract_bounded(xlsx_bytes(rows=rows), source=DocumentSource.XLSX)


@pytest.mark.parametrize(
    ("data", "source"),
    [
        (docx_bytes(paragraphs=["x"]), DocumentSource.XLSX),
        (xlsx_bytes(), DocumentSource.DOCX),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old excel", DocumentSource.XLSX),
        (b"%PDF-1.4 not a page", DocumentSource.HTML),
        (b"plain words", DocumentSource.DOCX),
    ],
    ids=["docx-as-xlsx", "xlsx-as-docx", "legacy-xls", "pdf-as-html", "text-as-docx"],
)
async def test_bytes_of_another_format_are_refused(data: bytes, source: DocumentSource) -> None:
    with pytest.raises(FormatMismatchError) as refused:
        await extract_bounded(data, source=source)
    assert refused.value.error_code == "document_format_mismatch"
    assert refused.value.status_code == 422


async def test_office_files_are_parsed_only_in_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def record(*args: Any, **kwargs: Any) -> Any:
        started.append(tuple(str(a) for a in args))
        assert "OPENAI_API_KEY" not in kwargs["env"] and "DATABASE_URL" not in kwargs["env"]
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    for data, source in (
        (xlsx_bytes(), DocumentSource.XLSX),
        (docx_bytes(paragraphs=["Delivery takes a day."]), DocumentSource.DOCX),
        (html_text(paragraphs=["Delivery takes a day."]).encode(), DocumentSource.HTML),
    ):
        await extract_bounded(data, source=source)
    assert [args[0] for args in started] == [sys.executable] * 3
    assert all(args[1].endswith("office_extract_child.py") for args in started)
    assert [args[2] for args in started] == ["xlsx", "docx", "html"]


async def test_a_parse_past_the_wall_clock_is_killed_and_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def keep(*args: Any, **kwargs: Any) -> Any:
        process = await real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", keep)
    rows = [(f"SKU-{n}", f"Item {n}", n) for n in range(900)]
    with pytest.raises(DocumentLimitError, match="took too long"):
        await extract_bounded(
            xlsx_bytes(rows=rows), source=DocumentSource.XLSX, timeout_seconds=0.05
        )
    assert len(processes) == 1 and processes[0].returncode is not None


async def test_an_upload_refused_by_a_limit_is_never_stored(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    rows = [(f"SKU-{n}", f"Item {n}", f"{n} EGP") for n in range(MAX_TABLE_ROWS + 1)]
    with pytest.raises(ValidationError):
        await _upload(indexing, tenant_id, DocumentSource.CSV, csv_text(rows))
    async with indexing.database.session() as session:
        assert (
            await session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.tenant_id == tenant_id)
            )
        ).first() is None


def test_every_row_renders_with_its_header() -> None:
    text = extract_csv("SKU;Product;Price\nBK-35;Bookshelf model 35;4500\n;Lamp;\n")
    assert text.splitlines() == [
        "SKU: BK-35; Product: Bookshelf model 35; Price: 4500",
        "Product: Lamp",
    ]
    assert not re.search(r"; ;", text)
