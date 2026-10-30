"""The process an Excel, Word or HTML knowledge document is parsed in, and nothing else.

Run by `app.services.document_formats.extract_bounded` exactly as PDFs are run
by `extraction.extract_pdf_bounded` (RAG-02, RQ-21): a separate interpreter, the
file on stdin, one JSON object on stdout, an environment holding no secrets,
memory and CPU capped where the platform allows it, and a parent that kills it
when the wall clock runs out. It imports the standard library, `openpyxl` and
`python-docx`, and nothing from `app`.

An `.xlsx` or `.docx` is a zip of XML, which gives a hostile file two more ways
to be bigger than it looks. Both are refused here, **before either library reads
a byte of XML** (`_inspect_archive`):

* an archive that inflates - more entries, more inflated bytes, or a higher
  compression ratio for any one part than `knowledge_limits` allows;
* XML that expands - any part declaring a `DOCTYPE` or an `ENTITY`. A real
  workbook or document never declares one; a billion-laughs file always does.

Then the rows, columns, cells, sheets and characters are counted as they are
read, and the parse stops - refused, never truncated - the moment one passes its
limit. A formula is kept as the text of the formula (`=B2*1.14`), never
evaluated, and its cached result is not read: what a workbook last computed is
not something this file can vouch for (M-R59).

The answer is text in the shape the chunker expects: a catalogue's rows as
`Column: value; Column: value` lines, one per row, under a `## Sheet` heading
when a workbook has more than one; a document's headings as `#` lines, list
items as `- ` lines, paragraphs separated by blank lines and table rows as
`Column: value` lines.
"""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from typing import Any


class RefusedError(Exception):
    """A limit was passed; `code` is what the parent maps to a message."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


_XML_DECLARATIONS = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
# What a `<br>` becomes until the text is split into lines: a character no
# page writes as whitespace of its own.
_BREAK = "\u2028"
_REQUIRED_PART = {"xlsx": "xl/workbook.xml", "docx": "word/document.xml"}


def _limit_resources(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        resource: Any = importlib.import_module("resource")
    except ImportError:  # pragma: no cover - platform dependent
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ValueError, OSError):  # pragma: no cover - platform dependent
        return


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=True).encode("ascii"))
    sys.stdout.buffer.flush()


def _clean(text: str) -> str:
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _inspect_archive(data: bytes, kind: str, limits: dict[str, int]) -> None:
    """Refuse a zip that inflates, or XML that expands, before any parser sees it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise RefusedError("wrong_format") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > limits["max_entries"]:
            raise RefusedError("archive_too_large", entries=len(entries))
        if _REQUIRED_PART[kind] not in archive.namelist():
            raise RefusedError("wrong_format")
        inflated = 0
        for entry in entries:
            inflated += entry.file_size
            if inflated > limits["max_uncompressed"]:
                raise RefusedError("archive_too_large")
            if entry.compress_size and entry.file_size / entry.compress_size > limits["max_ratio"]:
                raise RefusedError("archive_too_large")
        for entry in entries:
            if not entry.filename.endswith((".xml", ".rels", ".vml")):
                continue
            # Read in bounded pieces: the size the header claims is checked
            # above, and a part that inflates past it is refused here too.
            seen = 0
            tail = b""
            with archive.open(entry) as part:
                while piece := part.read(65_536):
                    seen += len(piece)
                    if seen > entry.file_size or seen > limits["max_uncompressed"]:
                        raise RefusedError("archive_too_large")
                    if _XML_DECLARATIONS.search(tail + piece):
                        raise RefusedError("xml_declarations")
                    tail = piece[-16:]


class _Text:
    """The answer being built, counted as it grows."""

    def __init__(self, max_characters: int) -> None:
        self.lines: list[str] = []
        self.size = 0
        self.max_characters = max_characters

    def add(self, line: str) -> None:
        self.size += len(line) + 1
        if self.size > self.max_characters:
            raise RefusedError("too_much_text")
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines).strip()


def cell_text(value: Any) -> str:
    """One cell as the text a customer would read; CSV cells go through this too."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    return _SPACE.sub(" ", str(value)).strip()


def render_row(header: list[str], cells: list[str]) -> str:
    """`Column: value; Column: value`, empty cells left out; CSV rows go through this too."""
    parts = []
    for index, value in enumerate(cells):
        if not value:
            continue
        name = header[index] if index < len(header) and header[index] else f"Column {index + 1}"
        parts.append(f"{name}: {value}")
    return "; ".join(parts)


def _last_filled(cells: list[str]) -> int:
    for index in range(len(cells) - 1, -1, -1):
        if cells[index]:
            return index + 1
    return 0


def _xlsx(data: bytes, limits: dict[str, int], answer: _Text) -> dict[str, int]:
    from openpyxl import load_workbook

    # `data_only=False`: a formula cell reads as its formula. Its cached value
    # is never read, and nothing here evaluates anything (M-R59).
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    formulas = 0
    rows_read = 0
    sheets: list[tuple[str, list[str]]] = []
    try:
        for index, sheet in enumerate(workbook.worksheets):
            if index >= limits["max_sheets"]:
                raise RefusedError("too_many_sheets", sheets=len(workbook.worksheets))
            header: list[str] | None = None
            lines: list[str] = []
            for raw in sheet.iter_rows(values_only=True, max_col=limits["max_columns"] + 1):
                cells = [cell_text(value) for value in raw]
                width = _last_filled(cells)
                if not width:
                    continue
                if width > limits["max_columns"]:
                    raise RefusedError("too_many_columns", columns=width)
                for value in cells:
                    if len(value) > limits["max_cell"]:
                        raise RefusedError("cell_too_large")
                    if value.startswith("="):
                        formulas += 1
                if header is None:
                    header = cells[:width]
                    continue
                rows_read += 1
                if rows_read > limits["max_rows"]:
                    raise RefusedError("too_many_rows")
                lines.append(render_row(header, cells[:width]))
            if lines:
                sheets.append((sheet.title, lines))
    finally:
        workbook.close()
    for title, lines in sheets:
        if len(sheets) > 1:
            # No ": " in a heading line: that is how the row chunker tells a
            # sheet heading from a row (every row holds "Column: value").
            answer.add(f"## {_SPACE.sub(' ', title.replace(':', ' ')).strip() or 'Sheet'}")
        for line in lines:
            answer.add(line)
        answer.add("")
    return {"formula_cells": formulas} if formulas else {}


def _docx(data: bytes, limits: dict[str, int], answer: _Text) -> dict[str, int]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = _SPACE.sub(" ", paragraph.text).strip()
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = re.match(r"Heading (\d)", style)
            if style == "Title":
                answer.add(f"# {text}")
            elif level:
                answer.add(f"{'#' * min(int(level.group(1)) + 1, 6)} {text}")
            elif "List" in style:
                answer.add(f"- {text}")
                continue
            else:
                answer.add(text)
            answer.add("")
        elif tag == "tbl":
            table = Table(child, document)
            header: list[str] | None = None
            rows = 0
            for row in table.rows:
                cells = [_SPACE.sub(" ", cell.text).strip() for cell in row.cells]
                width = _last_filled(cells)
                if not width:
                    continue
                if width > limits["max_columns"]:
                    raise RefusedError("too_many_columns", columns=width)
                if header is None:
                    header = cells[:width]
                    continue
                rows += 1
                if rows > limits["max_rows"]:
                    raise RefusedError("too_many_rows")
                answer.add(render_row(header, cells[:width]))
            if header is not None and rows == 0:
                answer.add(" | ".join(value for value in header if value))
            answer.add("")
    return {}


class _HtmlText(HTMLParser):
    """Visible text of an HTML page, with its structure as the chunker reads it."""

    _DROPPED = frozenset({"script", "style", "noscript", "template", "head", "svg", "iframe"})
    _BLOCKS = frozenset(
        {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "main",
            "aside",
            "nav",
            "blockquote",
            "pre",
            "dl",
            "dt",
            "dd",
            "ul",
            "ol",
            "table",
            "form",
            "figure",
        }
    )

    def __init__(self, answer: _Text, limits: dict[str, int]) -> None:
        super().__init__(convert_charrefs=True)
        self.answer = answer
        self.limits = limits
        self.dropped = 0
        self.buffer: list[str] = []
        self.prefix = ""
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.header: list[str] | None = None
        self.rows = 0

    def _flush(self, blank: bool = True) -> None:
        # A `<br>` is a line break the author asked for (a FAQ's "Q:" and "A:"
        # lines); every other run of whitespace is the page's formatting.
        lines = [_SPACE.sub(" ", line).strip() for line in "".join(self.buffer).split(_BREAK)]
        lines = [line for line in lines if line]
        self.buffer = []
        for index, line in enumerate(lines):
            self.answer.add(f"{self.prefix if index == 0 else ''}{line}")
        if lines and blank:
            self.answer.add("")
        self.prefix = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROPPED:
            self.dropped += 1
            return
        if self.dropped:
            return
        if tag == "br":
            (self.cell if self.cell is not None else self.buffer).append(_BREAK)
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.prefix = "#" * int(tag[1]) + " "
        elif tag == "li":
            self._flush(blank=False)
            self.prefix = "- "
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag in self._BLOCKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROPPED:
            self.dropped = max(0, self.dropped - 1)
            return
        if self.dropped:
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in self._BLOCKS:
            self._flush()
            if tag == "table":
                self.header = None
                self.rows = 0
        elif tag == "li":
            self._flush(blank=False)
        elif tag in ("td", "th") and self.row is not None and self.cell is not None:
            value = _SPACE.sub(" ", "".join(self.cell)).strip()
            if len(value) > self.limits["max_cell"]:
                raise RefusedError("cell_too_large")
            self.row.append(value)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells, self.row = self.row, None
            width = _last_filled(cells)
            if not width:
                return
            if width > self.limits["max_columns"]:
                raise RefusedError("too_many_columns", columns=width)
            if self.header is None:
                self.header = cells[:width]
                return
            self.rows += 1
            if self.rows > self.limits["max_rows"]:
                raise RefusedError("too_many_rows")
            self.answer.add(render_row(self.header, cells[:width]))

    def handle_data(self, data: str) -> None:
        if self.dropped:
            return
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _html(data: bytes, limits: dict[str, int], answer: _Text) -> dict[str, int]:
    parser = _HtmlText(answer, limits)
    parser.feed(data.decode("utf-8", "replace"))
    parser.close()
    return {}


_PARSERS = {"xlsx": _xlsx, "docx": _docx, "html": _html}


def extract(data: bytes, *, kind: str, limits: dict[str, int]) -> dict[str, Any]:
    """The answer for one file, as the dictionary the parent decodes."""
    answer = _Text(limits["max_characters"])
    try:
        if kind in _REQUIRED_PART:
            _inspect_archive(data, kind, limits)
        warnings = _PARSERS[kind](data, limits, answer)
    except RefusedError as refused:
        return {"ok": False, "code": refused.code, **refused.details}
    except MemoryError:
        return {"ok": False, "code": "too_much_text"}
    except Exception:
        return {"ok": False, "code": "unreadable"}
    return {"ok": True, "text": _clean(answer.text()), "warnings": warnings}


def main(argv: list[str]) -> int:
    kind = argv[1]
    limits = {key: int(value) for key, value in json.loads(argv[2]).items()}
    _limit_resources(limits["memory_bytes"], limits["cpu_seconds"])
    data = sys.stdin.buffer.read(limits["max_bytes"] + 1)
    if len(data) > limits["max_bytes"]:
        _emit({"ok": False, "code": "too_many_bytes"})
        return 0
    _emit(extract(data, kind=kind, limits=limits))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
