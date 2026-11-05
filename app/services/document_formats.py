"""Catalogues and office documents as knowledge (RQ-21, FX-3.17, FX-4.5, IMP-07).

A workspace keeps its price list in Excel and its policies in Word, and used to
have to retype both. Four formats join text, Markdown and PDF:

- **CSV** - parsed here with the standard library: `csv` is a linear pass in C
  over at most the upload's bytes, and cannot amplify.
- **XLSX, DOCX and HTML** - parsed in `office_extract_child`, the same kind of
  secret-free, killable child process a PDF is parsed in, under the same wall
  clock and memory ceiling plus archive, row, column, cell and sheet limits
  (`knowledge_limits`). HTML has no archive to inflate, but the standard
  library's HTML parser has had quadratic inputs, and a parser that can be slow
  on a stranger's file belongs where it can be killed.

**A catalogue row is one chunk.** CSV and XLSX come out as one
`Column: value; Column: value` line per row - the header repeated in every row,
so a row means something on its own - and are chunked a row at a time (the
chunker's row mode). The document's title labels every row's embedding
(RQ-14). A price is never cut from its product, and "Bookshelf model 35" finds
its own row rather than a passage of thirty rows that mentions it.

**Refused, never truncated** - a file past any limit is refused with the limit
in the message, as a 41-page PDF is (RAG-08). A file whose bytes are not the
format it was declared as is refused too: an `.xlsx` that is really a Word file,
a CSV that is a zip.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import sys
from pathlib import Path
from typing import Final

from app.core.exceptions import ValidationError, WaslaError
from app.core.logging import get_logger
from app.db.models.knowledge import DocumentSource
from app.services import extraction
from app.services.extraction import DocumentTooLargeError, UnreadableDocumentError
from app.services.knowledge_limits import (
    EXTRACTION_MEMORY_BYTES,
    EXTRACTION_TIMEOUT_SECONDS,
    MAX_ARCHIVE_COMPRESSION_RATIO,
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    MAX_BINARY_DOCUMENT_BYTES,
    MAX_CELL_CHARACTERS,
    MAX_EXTRACTED_CHARACTERS,
    MAX_SHEETS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
)
from app.services.office_extract_child import cell_text, render_row

logger = get_logger(__name__)

CHILD_SCRIPT: Final = Path(__file__).with_name("office_extract_child.py")

#: Formats that arrive as bytes: base64 in a JSON body, raw in a multipart one.
BINARY_SOURCES: Final = frozenset({DocumentSource.PDF, DocumentSource.XLSX, DocumentSource.DOCX})
#: Formats chunked a row at a time.
ROW_SOURCES: Final = frozenset({DocumentSource.CSV, DocumentSource.XLSX})
#: Formats whose extracted text marks headings with `#` lines.
MARKDOWN_HEADED_SOURCES: Final = frozenset(
    {DocumentSource.MARKDOWN, DocumentSource.DOCX, DocumentSource.HTML}
)

#: The media type an original is kept under in the object store (RQ-23).
MEDIA_TYPES: Final[dict[DocumentSource, str]] = {
    DocumentSource.TEXT: "text/plain",
    DocumentSource.MARKDOWN: "text/plain",
    DocumentSource.PDF: "application/pdf",
    DocumentSource.CSV: "text/csv",
    DocumentSource.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocumentSource.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentSource.HTML: "text/html",
}

_NAMES: Final[dict[DocumentSource, str]] = {
    DocumentSource.CSV: "CSV file",
    DocumentSource.XLSX: "Excel workbook (.xlsx)",
    DocumentSource.DOCX: "Word document (.docx)",
    DocumentSource.HTML: "HTML page",
}
_ZIP: Final = b"PK\x03\x04"
_PDF: Final = b"%PDF"
# The compound-file header of the old binary Excel and Word formats.
_LEGACY_OFFICE: Final = b"\xd0\xcf\x11\xe0"
_DELIMITERS: Final = ",;\t|"


class FormatMismatchError(UnreadableDocumentError):
    """The bytes are not the format the document was declared as. `422`."""

    error_code = "document_format_mismatch"


class DocumentLimitError(DocumentTooLargeError):
    """A catalogue or office file past one of its limits, refused - never truncated. `422`."""

    error_code = "document_limit_exceeded"


class UploadTooLargeError(WaslaError):
    """A multipart upload larger than any knowledge document may be. `413`."""

    status_code = 413
    error_code = "document_too_large"
    message = "That file is too large to upload."


def child_limits() -> dict[str, int]:
    """Every bound the child enforces, from `knowledge_limits` alone."""
    return {
        "max_characters": MAX_EXTRACTED_CHARACTERS,
        "max_bytes": MAX_BINARY_DOCUMENT_BYTES,
        "memory_bytes": EXTRACTION_MEMORY_BYTES,
        "cpu_seconds": int(EXTRACTION_TIMEOUT_SECONDS) + 1,
        "max_entries": MAX_ARCHIVE_ENTRIES,
        "max_uncompressed": MAX_ARCHIVE_UNCOMPRESSED_BYTES,
        "max_ratio": MAX_ARCHIVE_COMPRESSION_RATIO,
        "max_rows": MAX_TABLE_ROWS,
        "max_columns": MAX_TABLE_COLUMNS,
        "max_cell": MAX_CELL_CHARACTERS,
        "max_sheets": MAX_SHEETS,
    }


def check_declared_format(content: bytes, source: DocumentSource) -> None:
    """Refuse bytes that are plainly another format than the one declared.

    The child checks the archive's own parts too (an `.xlsx` must hold a
    workbook); this catches the cheap cases before a process is started.
    """
    name = _NAMES.get(source)
    if name is None:
        return
    if content.startswith(_LEGACY_OFFICE):
        raise FormatMismatchError(
            "That file is in the old binary Office format. "
            f"Save it as a {name} and upload it again."
        )
    binary = source in (DocumentSource.XLSX, DocumentSource.DOCX)
    if binary and not content.startswith(_ZIP):
        raise FormatMismatchError(f"That file is not a {name}.")
    if not binary and (content.startswith(_ZIP) or content.startswith(_PDF)):
        raise FormatMismatchError(f"That file is not a {name}.")


async def extract_bounded(
    content: bytes,
    *,
    source: DocumentSource,
    timeout_seconds: float = EXTRACTION_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, int]]:
    """An XLSX, DOCX or HTML file's text and warnings, parsed in a killable child.

    The same discipline as `extraction.extract_pdf_pages_bounded`: one of
    `MAX_CONCURRENT_EXTRACTIONS` slots, no secrets in the child's environment,
    a kill at the wall clock, and anything but a well-formed answer read as a
    file that could not be read.
    """
    if not content:
        raise UnreadableDocumentError()
    if len(content) > MAX_BINARY_DOCUMENT_BYTES:
        raise DocumentLimitError(
            f"That file is too large. The limit is {MAX_BINARY_DOCUMENT_BYTES // 1024} KB."
        )
    check_declared_format(content, source)
    async with extraction._slots():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CHILD_SCRIPT),
            str(source),
            json.dumps(child_limits()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=extraction._child_environment(),
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(content), timeout=timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "knowledge.extraction_timed_out",
                extra={"source": str(source), "timeout_seconds": timeout_seconds},
            )
            raise DocumentLimitError(
                f"That {_NAMES[source]} took too long to read. Split it into smaller files."
            ) from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    return _decode(output, source=source)


def _decode(output: bytes, *, source: DocumentSource) -> tuple[str, dict[str, int]]:
    if not output or len(output) > extraction.MAX_CHILD_OUTPUT_BYTES:
        logger.warning("knowledge.extraction_crashed", extra={"source": str(source)})
        raise UnreadableDocumentError()
    try:
        answer = json.loads(output)
    except ValueError:
        raise UnreadableDocumentError() from None
    if not isinstance(answer, dict):
        raise UnreadableDocumentError()
    text = answer.get("text")
    warnings = answer.get("warnings")
    if answer.get("ok") is True and isinstance(text, str):
        counts = (
            {str(k): int(v) for k, v in warnings.items() if isinstance(v, int)}
            if isinstance(warnings, dict)
            else {}
        )
        return text, counts
    raise refusal(str(answer.get("code")), source=source, details=answer)


def refusal(
    code: str, *, source: DocumentSource, details: dict[str, object] | None = None
) -> ValidationError:
    """The error a refused parse becomes - one message per limit, with the limit in it."""
    name = _NAMES.get(source, "file")
    details = details or {}
    messages = {
        "too_many_rows": (
            f"That {name} has more than {MAX_TABLE_ROWS} rows. Split it into files of at most "
            f"{MAX_TABLE_ROWS} rows so every row is searchable."
        ),
        "too_many_columns": (
            f"That {name} has {details.get('columns', 'too many')} columns. "
            f"The limit is {MAX_TABLE_COLUMNS}."
        ),
        "cell_too_large": (
            f"A cell of that {name} holds more than {MAX_CELL_CHARACTERS} characters. "
            "Put long text in a document instead."
        ),
        "too_many_sheets": f"That {name} has more than {MAX_SHEETS} sheets.",
        "too_much_text": (
            f"That {name} contains more text than can be indexed. "
            f"The limit is {MAX_EXTRACTED_CHARACTERS} characters."
        ),
        "too_many_bytes": (
            f"That file is too large. The limit is {MAX_BINARY_DOCUMENT_BYTES // 1024} KB."
        ),
        "archive_too_large": f"That {name} expands to more than can be read safely.",
        "xml_declarations": f"That {name} declares XML entities, which are not accepted.",
    }
    if code == "wrong_format":
        return FormatMismatchError(f"That file is not a {name}.")
    message = messages.get(code)
    if message is None:
        logger.warning("knowledge.document_unreadable", extra={"source": str(source)})
        return UnreadableDocumentError()
    logger.info("knowledge.document_refused", extra={"source": str(source), "code": code})
    return DocumentLimitError(message)


def refusal_code(error: BaseException) -> str:
    """A closed label for a refusal metric: never the message, never the file."""
    if isinstance(error, FormatMismatchError):
        return "format_mismatch"
    if isinstance(error, DocumentTooLargeError):
        return "limit"
    return "unreadable"


_EXTENSIONS: Final[dict[str, DocumentSource]] = {
    ".txt": DocumentSource.TEXT,
    ".md": DocumentSource.MARKDOWN,
    ".markdown": DocumentSource.MARKDOWN,
    ".pdf": DocumentSource.PDF,
    ".csv": DocumentSource.CSV,
    ".xlsx": DocumentSource.XLSX,
    ".docx": DocumentSource.DOCX,
    ".html": DocumentSource.HTML,
    ".htm": DocumentSource.HTML,
}
#: The most a multipart upload may carry: a binary file at its limit, or text
#: that could decode to the character limit at four bytes a character.
MAX_TEXT_UPLOAD_BYTES: Final = MAX_EXTRACTED_CHARACTERS * 4


def upload_source(declared: DocumentSource | None, filename: str | None) -> DocumentSource:
    """A multipart upload's format: as declared, else from its extension, else text."""
    if declared is not None:
        return declared
    suffix = Path(filename or "").suffix.lower()
    return _EXTENSIONS.get(suffix, DocumentSource.TEXT)


def upload_as_submission(content: bytes, *, source: DocumentSource) -> str:
    """A multipart file as the `content` the JSON route would have carried (RQ-22).

    Binary formats as base64, text formats decoded - so both routes run one
    pipeline with one set of limits, and a refusal reads the same from either.
    """
    if not content:
        raise UnreadableDocumentError("The uploaded file is empty.")
    limit = MAX_BINARY_DOCUMENT_BYTES if source in BINARY_SOURCES else MAX_TEXT_UPLOAD_BYTES
    if len(content) > limit:
        raise UploadTooLargeError(f"That file is too large. The limit is {limit // 1024} KB.")
    check_declared_format(content, source)
    if source in BINARY_SOURCES:
        return base64.b64encode(content).decode("ascii")
    if source is DocumentSource.CSV:
        return decode_csv_bytes(content)
    return extraction.extract_text(content)


def decode_csv_bytes(content: bytes) -> str:
    """A CSV's text: UTF-16 without a byte-order mark recognised by its NULs, else `extract_text`.

    Excel's "Unicode Text" export is UTF-16 little-endian, often without a
    mark; read as UTF-8 it is every character followed by a NUL.
    """
    sample = content[:4_096]
    if sample and b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        odd = sample[1::2].count(0)
        even = sample[0::2].count(0)
        half = len(sample) // 2
        if half and odd / half > 0.3 and odd > even:
            return content.decode("utf-16-le", "replace").strip()
        if half and even / half > 0.3 and even > odd:
            return content.decode("utf-16-be", "replace").strip()
    return extraction.extract_text(content)


def extract_csv(text: str) -> str:
    """A CSV as one `Column: value; …` line per row, under the same limits as a workbook."""
    check_declared_format(text[:8].encode("utf-8", "replace"), DocumentSource.CSV)
    sample = text[:16_384]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, _DELIMITERS)
    except csv.Error:
        dialect = csv.excel
    lines: list[str] = []
    header: list[str] | None = None
    rows = 0
    size = 0
    try:
        for raw in csv.reader(io.StringIO(text), dialect):
            cells = [cell_text(value) for value in raw]
            width = 0
            for index in range(len(cells) - 1, -1, -1):
                if cells[index]:
                    width = index + 1
                    break
            if not width:
                continue
            if width > MAX_TABLE_COLUMNS:
                raise refusal(
                    "too_many_columns", source=DocumentSource.CSV, details={"columns": width}
                )
            if any(len(value) > MAX_CELL_CHARACTERS for value in cells):
                raise refusal("cell_too_large", source=DocumentSource.CSV)
            if header is None:
                header = cells[:width]
                continue
            rows += 1
            if rows > MAX_TABLE_ROWS:
                raise refusal("too_many_rows", source=DocumentSource.CSV)
            line = render_row(header, cells[:width])
            size += len(line) + 1
            if size > MAX_EXTRACTED_CHARACTERS:
                raise refusal("too_much_text", source=DocumentSource.CSV)
            lines.append(line)
    except csv.Error:
        # The module's own field-size bound, or a malformed quote.
        raise refusal("cell_too_large", source=DocumentSource.CSV) from None
    return "\n".join(lines)


__all__ = [
    "BINARY_SOURCES",
    "MARKDOWN_HEADED_SOURCES",
    "MAX_TEXT_UPLOAD_BYTES",
    "MEDIA_TYPES",
    "ROW_SOURCES",
    "DocumentLimitError",
    "FormatMismatchError",
    "UploadTooLargeError",
    "check_declared_format",
    "child_limits",
    "decode_csv_bytes",
    "extract_bounded",
    "extract_csv",
    "refusal",
    "refusal_code",
    "upload_as_submission",
    "upload_source",
]
