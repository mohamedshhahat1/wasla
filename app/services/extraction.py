"""Getting text out of documents.

Two callers, one implementation. A customer sends a PDF over WhatsApp and an
agent needs to know what it says; a workspace uploads a PDF to its knowledge base
and it needs to be searchable. Both want the same thing - and both now get it the
same way: parsed in a killable child process with byte, page, text, memory and
time limits (`extract_pdf_bounded`, RAG-02).

The message path used to have its own in-process `extract_pdf`, which ran
`pypdf` on the event loop every worker in the process shares. A 25 KB customer
PDF stalled that loop for 170 seconds and took it to 763 MB (MEDIA-02), and
parser exceptions outside its catch list escaped it altogether (MEDIA-03). It is
gone rather than fixed: nothing in this process parses a stranger's PDF.

What this deliberately does not do is OCR. A PDF that is a photograph of a
contract has no text layer, and this returns nothing rather than a page of
ligature noise - which the caller can then report honestly instead of indexing
gibberish that answers questions wrongly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import weakref
from pathlib import Path
from typing import Final

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.services.knowledge_limits import (
    EXTRACTION_MEMORY_BYTES,
    EXTRACTION_TIMEOUT_SECONDS,
    MAX_CONCURRENT_EXTRACTIONS,
    MAX_EXTRACTED_CHARACTERS,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
)

logger = get_logger(__name__)

# The standalone parser `extract_pdf_bounded` runs. A path rather than a module
# name, so the child never needs `app` on its import path.
CHILD_SCRIPT: Final = Path(__file__).with_name("pdf_extract_child.py")
# The environment variables a child may inherit. An allow-list, so the process
# reading a stranger's file never holds the OpenAI key, the Meta token or the
# database password. The Windows entries are what its runtime needs to start.
CHILD_ENVIRONMENT: Final = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL")
# More than the largest answer the child can legitimately give: the character
# limit, escaped as ASCII JSON at worst six bytes a character, plus the envelope.
MAX_CHILD_OUTPUT_BYTES: Final = MAX_EXTRACTED_CHARACTERS * 6 + 4_096

PDF_TYPE: Final = "application/pdf"
TEXT_TYPE: Final = "text/plain"

# Text that arrives as bytes has no declared encoding worth trusting. These are
# tried in order, and the last one cannot fail, so decoding always terminates.
TEXT_ENCODINGS: Final = ("utf-8", "utf-16", "cp1256", "latin-1")


class UnreadableDocumentError(ValidationError):
    """The bytes are not a document this can read."""

    message = "This document could not be read."


class DocumentTooLargeError(ValidationError):
    """A document past one of the knowledge limits (`knowledge_limits`)."""

    message = "This document is larger than can be indexed."


# One semaphore per event loop. A module-level `asyncio.Semaphore` binds to the
# first loop that waits on it, and a process that runs more than one loop - a
# test suite, a worker restarting its loop - would then fail on the second.
_extraction_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _extraction_slots.get(loop)
    if slots is None:
        slots = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
        _extraction_slots[loop] = slots
    return slots


def _child_environment() -> dict[str, str]:
    return {name: os.environ[name] for name in CHILD_ENVIRONMENT if name in os.environ}


async def extract_pdf_bounded(
    content: bytes,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_characters: int = MAX_EXTRACTED_CHARACTERS,
    timeout_seconds: float = EXTRACTION_TIMEOUT_SECONDS,
) -> str:
    """The text layer of a PDF, parsed in a process that can be killed.

    The only PDF parser this application runs. The knowledge base uses it for
    documents whose text becomes chunks somebody pays to embed (RAG-02), and the
    media reader uses it, with the same limits, for a PDF a customer sent
    (MEDIA-02). Never runs `pypdf` on the calling event loop, and never lets
    it run past `timeout_seconds`: the parse happens in `pdf_extract_child`,
    which enforces the page and character limits itself, and which this kills if
    the clock runs out first.

    Refuses rather than truncates. A PDF over the page limit, or one whose text
    would exceed the character limit, raises `DocumentTooLargeError` with the
    limit in the message; the audit's 60-page PDF was indexed to page 40 and
    reported `ready` (RAG-08). A scanned PDF returns an empty string, which is a
    real answer rather than a failure: a valid PDF with no text layer.

    A crash of the child - whatever the parser raised, and it raises far more
    than its own error type on hostile input - arrives here as no answer and
    becomes `UnreadableDocumentError`. The parent never sees the exception.
    """
    if not content:
        raise UnreadableDocumentError()
    if len(content) > MAX_PDF_BYTES:
        raise DocumentTooLargeError(
            f"That PDF is too large. The limit is {MAX_PDF_BYTES // 1024} KB."
        )

    async with _slots():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CHILD_SCRIPT),
            str(max_pages),
            str(max_characters),
            str(MAX_PDF_BYTES),
            str(EXTRACTION_MEMORY_BYTES),
            str(int(timeout_seconds) + 1),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_child_environment(),
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(content), timeout=timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "knowledge.pdf_extraction_timed_out",
                extra={"timeout_seconds": timeout_seconds},
            )
            raise DocumentTooLargeError(
                "That PDF took too long to read. Split it into smaller documents "
                "or submit its text directly."
            ) from None
        finally:
            # On a timeout, a cancelled request or any other exit that did not
            # wait for the child: the parse must not outlive the caller.
            if process.returncode is None:
                process.kill()
                await process.wait()

    return _decode_child_answer(output, max_pages=max_pages, max_characters=max_characters)


def _decode_child_answer(output: bytes, *, max_pages: int, max_characters: int) -> str:
    if not output or len(output) > MAX_CHILD_OUTPUT_BYTES:
        # A crash, an exhausted memory limit or a kill by the operating system.
        logger.warning("knowledge.pdf_extraction_crashed")
        raise UnreadableDocumentError()
    try:
        answer = json.loads(output)
    except ValueError:
        raise UnreadableDocumentError() from None
    if not isinstance(answer, dict):
        raise UnreadableDocumentError()

    if answer.get("ok") is True and isinstance(answer.get("text"), str):
        text: str = answer["text"]
        if len(text) > max_characters:
            raise DocumentTooLargeError(
                f"That PDF contains more text than can be indexed. "
                f"The limit is {max_characters} characters."
            )
        return text

    code = answer.get("code")
    if code == "too_many_pages":
        pages = answer.get("pages")
        counted = f" has {pages} pages" if isinstance(pages, int) else " has too many pages"
        raise DocumentTooLargeError(
            f"That PDF{counted}. The limit is {max_pages} pages; split it into "
            "smaller documents so every page is searchable."
        )
    if code == "too_much_text":
        raise DocumentTooLargeError(
            f"That PDF contains more text than can be indexed. "
            f"The limit is {max_characters} characters."
        )
    if code == "too_many_bytes":
        raise DocumentTooLargeError(
            f"That PDF is too large. The limit is {MAX_PDF_BYTES // 1024} KB."
        )
    logger.warning("media.pdf_unreadable")
    raise UnreadableDocumentError()


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


async def extract_document(*, content: bytes, mime_type: str | None) -> str:
    """Text from a document of a supported type.

    A PDF goes to the bounded child and may raise `DocumentTooLargeError` as
    well as `UnreadableDocumentError`; plain text is decoded here, which is a
    linear pass over at most the media byte cap and cannot amplify.
    """
    kind = (mime_type or "").lower()
    if kind == PDF_TYPE:
        return await extract_pdf_bounded(content)
    if kind == TEXT_TYPE:
        return extract_text(content)
    raise UnreadableDocumentError()
