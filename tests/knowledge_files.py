"""Building the catalogue and office files the knowledge tests upload (RQ-21).

Real files, written by the same libraries a customer's software would be
imitated by - openpyxl for a workbook, python-docx for a Word document - and
hostile ones made by editing a real file's archive, so a refusal is proved
against the shape an attacker would actually send.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

import docx
from openpyxl import Workbook

PRICE_ROWS: list[tuple[str, str, str]] = [
    ("BK-35", "Bookshelf model 35", "4,500 EGP"),
    ("BK-36", "Bookshelf model 36", "4,700 EGP"),
    ("DS-12", "Desk model 12", "3,200 EGP"),
    ("WR-07", "Wardrobe model 7", "11,900 EGP"),
    ("SF-03", "Sofa model 3", "8,400 EGP"),
]
PRICE_HEADER: tuple[str, str, str] = ("SKU", "Product", "Price")


def csv_text(rows: Sequence[Sequence[str]], *, header: Sequence[str] = PRICE_HEADER) -> str:
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(f'"{value}"' if "," in value else value for value in row))
    return "\n".join(lines) + "\n"


def xlsx_bytes(
    sheets: dict[str, Sequence[Sequence[object]]] | None = None,
    *,
    rows: Sequence[Sequence[object]] | None = None,
    header: Sequence[str] = PRICE_HEADER,
) -> bytes:
    workbook = Workbook()
    first = workbook.active
    assert first is not None
    named = sheets or {"Prices": [header, *(rows if rows is not None else PRICE_ROWS)]}
    for index, (title, content) in enumerate(named.items()):
        sheet = first if index == 0 else workbook.create_sheet()
        sheet.title = title
        for row in content:
            sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def docx_bytes(
    *,
    title: str = "Delivery and returns",
    paragraphs: Sequence[str] = (),
    faq: Sequence[tuple[str, str]] = (),
    table: Sequence[Sequence[str]] | None = None,
) -> bytes:
    document = docx.Document()
    document.add_heading(title, level=1)
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    for question, answer in faq:
        document.add_paragraph(f"Q: {question}")
        document.add_paragraph(f"A: {answer}")
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def html_text(
    *, title: str = "Delivery", paragraphs: Sequence[str] = (), faq: Sequence[tuple[str, str]] = ()
) -> str:
    body = [f"<h1>{title}</h1>"]
    body += [f"<p>{p}</p>" for p in paragraphs]
    for question, answer in faq:
        body.append(f"<p>Q: {question}<br>\nA: {answer}</p>")
    return (
        "<!doctype html><html><head><title>t</title><style>p{color:red}</style>"
        "<script>var secret = 'SCRIPT-TEXT';</script></head><body>"
        + "\n".join(body)
        + "</body></html>"
    )


def rewrite_part(data: bytes, name: str, content: bytes, *, compress: bool = True) -> bytes:
    """A copy of an archive with one part replaced or added."""
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", method) as target:
        for entry in source.infolist():
            if entry.filename != name:
                target.writestr(entry.filename, source.read(entry.filename))
        target.writestr(name, content)
    return buffer.getvalue()


def read_part(data: bytes, name: str) -> bytes:
    return zipfile.ZipFile(io.BytesIO(data)).read(name)


def zip_bomb_xlsx(inflated_bytes: int = 8 * 1024 * 1024) -> bytes:
    """A real workbook carrying one part that inflates over a thousandfold."""
    return rewrite_part(xlsx_bytes(), "xl/media/padding.xml", b"\x00" * inflated_bytes)


def entity_expansion_docx() -> bytes:
    """A real Word document whose body declares the billion-laughs entities."""
    original = docx_bytes(paragraphs=["Hello"])
    lol = (
        b'<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol">'
        + b"".join(
            f'<!ENTITY lol{n} "{("&lol" + (str(n - 1) if n > 1 else "") + ";") * 10}">'.encode()
            for n in range(1, 10)
        )
        + b"]>\n"
    )
    body = read_part(original, "word/document.xml")
    body = body.split(b"?>", 1)[1] if body.startswith(b"<?xml") else body
    return rewrite_part(original, "word/document.xml", lol + body.replace(b"Hello", b"&lol9;"))


def formula_xlsx() -> bytes:
    """A workbook with a formula whose cached result a reader might be tempted to trust."""
    data = xlsx_bytes(
        rows=[
            ("BK-35", "Bookshelf model 35", "=B2*2"),
            ("LNK-1", "Catalogue link", '=HYPERLINK("http://example.test","open")'),
        ]
    )
    sheet = read_part(data, "xl/worksheets/sheet1.xml")
    # Give the first formula a cached value, as Excel would have written it.
    sheet = sheet.replace(b"<f>B2*2</f><v></v>", b"<f>B2*2</f><v>9999</v>", 1)
    assert b"<v>9999</v>" in sheet
    return rewrite_part(data, "xl/worksheets/sheet1.xml", sheet)


__all__ = [
    "PRICE_HEADER",
    "PRICE_ROWS",
    "csv_text",
    "docx_bytes",
    "entity_expansion_docx",
    "formula_xlsx",
    "html_text",
    "read_part",
    "rewrite_part",
    "xlsx_bytes",
    "zip_bomb_xlsx",
]
