"""The structural evaluation of new formats (RAG remediation part 3).

Same rules as `rag_eval`: fake embeddings, production code above the socket,
the real worker draining a real queue. A workspace of its own, built from files
in every format a business keeps its catalogue and its answers in:

- **catalogue rows**: a CSV and an XLSX price list, sixty rows each (English
  and Arabic product names, SKUs, prices in both digit scripts). Each row is
  asked for by its product name; the row is *exact* when the first passage is
  that row and no other;
- **FAQ pairs**: the same six questions written as plain text, Markdown, DOCX,
  HTML and PDF. Each question is *matched* when the first passage holds its
  own answer and no other question;
- **spans intact**: every row's price and every answer present whole in some
  chunk;
- **stale**: the CSV list replaced by one with every price changed; a row is
  stale when its old price appears in the top five;
- **duplicates**: identical passages in one search's results, averaged.

Written to run unchanged against the base of this Part (b9c91c2), where CSV,
XLSX, DOCX and HTML are refused: a refused file is reported, and its questions
count as not answered - which is the "before".
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models.knowledge import DocumentChunk
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.rag_harness import Indexing
from tests.knowledge_files import docx_bytes, html_text, xlsx_bytes
from tests.pdf_fixtures import _assemble, _stream

TOP_K = 5
_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_EN = [
    "Oak bookshelf",
    "Walnut desk",
    "Linen sofa",
    "Teak wardrobe",
    "Leather armchair",
    "Pine bed",
]
_AR = ["مكتبة بلوط", "مكتب جوز", "كنبة كتان", "دولاب ساج", "فوتيه جلد", "سرير صنوبر"]
FAQ = [
    ("How long does delivery to Cairo take?", "Delivery to Cairo takes one working day."),
    ("Can I return a sofa after delivery?", "Sofas can be returned within fourteen days, unused."),
    ("Is installation of wardrobes included?", "Wardrobe installation is free of charge."),
    (
        "Do you deliver to Alexandria on Fridays?",
        "Friday deliveries to Alexandria are not offered.",
    ),
    ("What warranty covers the mattresses?", "Mattresses carry a five year warranty."),
    ("Can I pay in instalments?", "Instalments over twelve months are available with a bank card."),
]


def _rows(prefix: str, offset: int) -> list[tuple[str, str, str]]:
    rows = []
    for n in range(60):
        names = _EN if n % 2 == 0 else _AR
        product = f"{names[n % 6]} {prefix}{n + offset}"
        price = f"{1000 + 37 * (n + offset):,} EGP"
        if n % 3 == 0:
            price = price.translate(_DIGITS)
        rows.append((f"{prefix}-{n + offset:04d}", product, price))
    return rows


CSV_ROWS = _rows("C", 0)
XLSX_ROWS = _rows("X", 100)


def _csv(rows: list[tuple[str, str, str]]) -> str:
    return "SKU,Product,Price\n" + "\n".join(f'{s},{p},"{c}"' for s, p, c in rows) + "\n"


def _faq_text(source: str) -> str:
    if source == "text":
        return "Questions\n\n" + "\n\n".join(f"Q: {q}\nA: {a}" for q, a in FAQ)
    if source == "markdown":
        return "# Questions\n\n" + "\n\n".join(f"{n}. {q}\n{a}" for n, (q, a) in enumerate(FAQ, 1))
    if source == "docx":
        return base64.b64encode(docx_bytes(title="Questions", faq=FAQ)).decode()
    if source == "html":
        return html_text(title="Questions", faq=FAQ)
    parts = []
    for question, answer in FAQ:
        parts.append(b"(Q: " + question.encode("latin-1") + b") Tj 0 -16 Td")
        parts.append(b"(A: " + answer.encode("latin-1") + b") Tj 0 -32 Td")
    content = b"BT /F1 11 Tf 72 740 Td " + b" ".join(parts) + b" ET"
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


@dataclass
class Part3Run:
    metrics: dict[str, Any] = field(default_factory=dict)

    def table(self, label: str) -> str:
        lines = [f"### {label} (part 3)", "", "| Metric (fake embeddings - structural) | Value |"]
        lines.append("|---|---|")
        lines += [f"| {k} | {v} |" for k, v in self.metrics.items() if not k.startswith("_")]
        return "\n".join(lines) + "\n"


async def _submit(
    indexing: Indexing, tenant_id: uuid.UUID, *, title: str, raw: str, source: str
) -> uuid.UUID | str:
    from app.db.models.knowledge import DocumentSource

    try:
        chosen = DocumentSource(source)
    except ValueError:
        return f"refused ({source} is not a source)"
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        try:
            view, _ = await knowledge.submit(
                knowledge_base_id=base.id, title=title, raw=raw, source=chosen
            )
        except Exception as error:
            return f"refused ({type(error).__name__})"
        return view.document.id


async def _replace(
    indexing: Indexing, tenant_id: uuid.UUID, document_id: uuid.UUID, raw: str, source: str
) -> None:
    from app.db.models.knowledge import DocumentSource

    async with indexing.database.session() as session:
        await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).replace_content(document_id, raw=raw, source=DocumentSource(source))


async def _first(indexing: Indexing, tenant_id: uuid.UUID, query: str) -> list[str]:
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=query, top_k=TOP_K, max_distance=2.0)
    return [passage.content for passage in found.passages]


async def measure_part3(indexing: Indexing) -> Part3Run:
    """Part 3's metrics, in a workspace of their own."""
    result = Part3Run()
    tenant_id = await indexing.workspace()
    documents: dict[str, uuid.UUID | str] = {
        "csv": await _submit(
            indexing, tenant_id, title="Price list", raw=_csv(CSV_ROWS), source="csv"
        ),
        "xlsx": await _submit(
            indexing,
            tenant_id,
            title="Catalogue",
            raw=base64.b64encode(xlsx_bytes(rows=XLSX_ROWS)).decode(),
            source="xlsx",
        ),
    }
    # Each FAQ format in a workspace of its own: the same pairs in two formats
    # of one workspace are near repeats, and Part 2 serves a repeat once.
    faq_workspaces: dict[str, uuid.UUID] = {}
    for source in ("text", "markdown", "docx", "html", "pdf"):
        faq_workspaces[source] = await indexing.workspace()
        documents[f"faq-{source}"] = await _submit(
            indexing,
            faq_workspaces[source],
            title="Questions",
            raw=_faq_text(source),
            source=source,
        )
    await indexing.drain(budget=50)
    result.metrics["documents"] = {
        name: ("indexed" if isinstance(value, uuid.UUID) else value)
        for name, value in documents.items()
    }

    async with indexing.database.session() as session:
        chunks = [
            row.content
            for row in await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.tenant_id.in_([tenant_id, *faq_workspaces.values()])
                )
            )
        ]

    exact = 0
    missed: list[str] = []
    duplicates = 0
    searches = 0
    for sku, product, price in CSV_ROWS + XLSX_ROWS:
        found = await _first(indexing, tenant_id, f"{product} price")
        searches += 1
        duplicates += len(found) - len(set(found))
        if found and sku in found[0] and price in found[0] and found[0].count("SKU:") == 1:
            exact += 1
        else:
            missed.append(sku)
    result.metrics["catalogue rows retrieved as the exact row (CSV / XLSX)"] = (
        f"{exact} / {len(CSV_ROWS) + len(XLSX_ROWS)}"
    )

    matched = 0
    for source, workspace in faq_workspaces.items():
        for question, answer in FAQ:
            found = await _first(indexing, workspace, question)
            searches += 1
            duplicates += len(found) - len(set(found))
            others = [q for q, _ in FAQ if q != question]
            if found and answer in found[0] and not any(q in found[0] for q in others):
                matched += 1
            else:
                missed.append(f"{source}: {question[:24]}")
    result.metrics["FAQ questions matched to their own answer (5 formats x 6)"] = (
        f"{matched} / {len(FAQ) * 5}"
    )

    spans = [price for _, _, price in CSV_ROWS + XLSX_ROWS] + [a for _, a in FAQ]
    intact = sum(1 for span in spans if any(span in chunk for chunk in chunks))
    result.metrics["labelled spans intact in some chunk (all formats)"] = f"{intact} / {len(spans)}"

    stale = "n/a (CSV refused)"
    csv_id = documents["csv"]
    if isinstance(csv_id, uuid.UUID):
        newer = [(s, p, f"{9000 + n:,} EGP") for n, (s, p, _) in enumerate(CSV_ROWS)]
        await _replace(indexing, tenant_id, csv_id, _csv(newer), "csv")
        await indexing.drain(budget=10)
        old = 0
        for (_, product, price), (_, _, new_price) in zip(CSV_ROWS, newer, strict=True):
            found = await _first(indexing, tenant_id, f"{product} price")
            searches += 1
            duplicates += len(found) - len(set(found))
            if any(price in passage for passage in found):
                old += 1
            assert any(new_price in passage for passage in found) or not found
        stale = f"{old} / {len(CSV_ROWS)}"
    result.metrics["questions with a stale version in the top results"] = stale
    result.metrics["missed (row SKUs, FAQ questions)"] = ", ".join(missed) or "none"
    result.metrics["duplicate passages per search"] = round(duplicates / max(searches, 1), 3)
    result.metrics["_raw"] = {"exact": exact, "matched": matched, "intact": intact}
    return result


__all__ = ["Part3Run", "measure_part3"]
