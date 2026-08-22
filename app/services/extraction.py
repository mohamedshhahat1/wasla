"""Getting text out of documents.

Two callers, one implementation. A customer sends a PDF over WhatsApp and an
agent needs to know what it says; a workspace uploads a PDF to its knowledge base
and it needs to be searchable. Both want the same thing, and `KnowledgeService`
has refused PDFs since phase 6 with a note saying a parser was not yet a
dependency. It is now.

What this deliberately does not do is OCR. A PDF that is a photograph of a
contract has no text layer, and this returns nothing rather than a page of
ligature noise - which the caller can then report honestly instead of indexing
gibberish that answers questions wrongly.
"""

from __future__ import annotations

import io
from typing import Final

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.core.exceptions import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Enough for a long brochure and far short of anything that would take a
# meaningful amount of time. A document past this is read up to the limit
# rather than refused: the first forty pages of a catalogue are worth more to
# an agent than an error.
MAX_PAGES: Final = 40

PDF_TYPE: Final = "application/pdf"
TEXT_TYPE: Final = "text/plain"

# Text that arrives as bytes has no declared encoding worth trusting. These are
# tried in order, and the last one cannot fail, so decoding always terminates.
TEXT_ENCODINGS: Final = ("utf-8", "utf-16", "cp1256", "latin-1")


class UnreadableDocumentError(ValidationError):
    """The bytes are not a document this can read."""

    message = "This document could not be read."


def extract_pdf(content: bytes) -> str:
    """Pull the text layer out of a PDF.

    Returns an empty string for a scanned document, which is a real answer
    rather than a failure: the file is a valid PDF and simply contains no text.
    Telling those apart matters, because one is worth reporting to the person
    who uploaded it and the other is worth retrying.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages[:MAX_PAGES]
        extracted = [page.extract_text() or "" for page in pages]
    except (PyPdfError, ValueError, OSError, RecursionError) as error:
        # RecursionError is in this list on purpose. A malformed cross-reference
        # table can send the parser into a cycle, and a customer's attachment is
        # exactly the kind of input that carries one.
        logger.warning("media.pdf_unreadable")
        raise UnreadableDocumentError() from error

    return "\n\n".join(part.strip() for part in extracted if part.strip()).strip()


def extract_text(content: bytes) -> str:
    """Decode a plain text file.

    Windows-1256 is in the list because it is what Arabic text saved from older
    Windows software arrives as, and this product's customers send exactly that.
    Latin-1 is last and accepts any byte sequence, so this never raises.
    """
    for encoding in TEXT_ENCODINGS:
        try:
            return content.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    # Unreachable: latin-1 maps every byte. Kept so a future edit to the list
    # cannot silently fall off the end of the function returning None.
    return content.decode("latin-1", errors="replace").strip()


def extract_document(*, content: bytes, mime_type: str | None) -> str:
    """Text from a document of a supported type."""
    kind = (mime_type or "").lower()
    if kind == PDF_TYPE:
        return extract_pdf(content)
    if kind == TEXT_TYPE:
        return extract_text(content)
    raise UnreadableDocumentError()
