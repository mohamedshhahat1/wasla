"""Getting text out of documents.

The PDFs here are built byte by byte rather than checked in as fixtures, so what
each test feeds the parser is visible in the test itself - including the
malformed ones, which are the interesting cases. A customer's attachment is
untrusted input, and this parser is the thing that opens it.
"""

from __future__ import annotations

import io
import zlib

import pytest
from pypdf import PdfWriter

from app.services import extraction
from app.services.extraction import (
    UnreadableDocumentError,
    extract_document,
    extract_pdf_bounded,
    extract_text,
)


def _pdf_with_text(text: str, *, pages: int = 1) -> bytes:
    """A minimal PDF whose content stream draws `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    compressed = zlib.compress(stream)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{4 + index * 2} 0 R".encode() for index in range(pages))
        + f"] /Count {pages}".encode()
        + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(pages):
        content_ref = 5 + index * 2
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
            b"/MediaBox [0 0 612 792] /Contents " + f"{content_ref} 0 R".encode() + b" >>"
        )
        objects.append(
            f"<< /Length {len(compressed)} /Filter /FlateDecode >>".encode()
            + b"\nstream\n"
            + compressed
            + b"\nendstream"
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n".encode()
        + b"%%EOF\n"
    )
    return out.getvalue()


def _blank_pdf() -> bytes:
    """A valid PDF with no text layer, as a scan would be."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def test_a_pdf_gives_up_its_text() -> None:
    assert "Invoice" in await extract_pdf_bounded(_pdf_with_text("Invoice 2026"))


async def test_a_scan_reads_as_empty_rather_than_failing() -> None:
    """The distinction that matters to whoever uploaded it.

    An empty result means a valid PDF with nothing to read - it needs OCR. A
    raised error means the bytes were not a PDF at all. Collapsing the two
    would tell that person to fix the wrong thing.
    """
    assert await extract_pdf_bounded(_blank_pdf()) == ""


async def test_something_that_is_not_a_pdf_is_refused() -> None:
    with pytest.raises(UnreadableDocumentError):
        await extract_pdf_bounded(b"this is not a pdf at all")


async def test_an_empty_file_is_refused() -> None:
    with pytest.raises(UnreadableDocumentError):
        await extract_pdf_bounded(b"")


async def test_a_truncated_pdf_is_refused_rather_than_crashing() -> None:
    """Half a file is exactly what an interrupted download leaves behind."""
    whole = _pdf_with_text("Invoice")
    with pytest.raises(UnreadableDocumentError):
        await extract_pdf_bounded(whole[: len(whole) // 3])


def test_there_is_no_in_process_pdf_parser_left_to_call() -> None:
    """MEDIA-02. The message path used to call an unbounded `extract_pdf` on
    the worker's event loop. Nothing of that name, and no `pypdf` import, is
    left in the parent process's extraction module to be called by mistake."""
    assert not hasattr(extraction, "extract_pdf")
    assert "pypdf" not in vars(extraction)
    assert "PdfReader" not in vars(extraction)


def test_utf8_text_is_decoded() -> None:
    assert extract_text("مرحبا".encode()) == "مرحبا"


def test_windows_1256_arabic_is_decoded() -> None:
    """What Arabic saved from older Windows software actually arrives as."""
    assert extract_text("مرحبا".encode("cp1256")) != ""


def test_decoding_never_raises() -> None:
    """Latin-1 is last and maps every byte, so this always terminates."""
    assert isinstance(extract_text(bytes(range(256))), str)


async def test_a_document_is_routed_by_type() -> None:
    assert await extract_document(content=b"hello", mime_type="text/plain") == "hello"
    assert "Invoice" in await extract_document(
        content=_pdf_with_text("Invoice"), mime_type="application/pdf"
    )


async def test_the_type_is_matched_case_insensitively() -> None:
    assert await extract_document(content=b"hello", mime_type="TEXT/PLAIN") == "hello"


async def test_an_unsupported_type_is_refused() -> None:
    with pytest.raises(UnreadableDocumentError):
        await extract_document(content=b"...", mime_type="application/vnd.ms-excel")


async def test_a_missing_type_is_refused() -> None:
    with pytest.raises(UnreadableDocumentError):
        await extract_document(content=b"...", mime_type=None)
