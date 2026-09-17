"""Synthetic PDFs, built byte by byte, for the extraction limits.

Hand-written rather than generated with a library so the suite controls exactly
what the parser meets: how many pages, how much text each one carries, and how
far a compressed content stream expands. Nothing here is a real customer
document.
"""

from __future__ import annotations

import zlib


def _assemble(objects: list[bytes]) -> bytes:
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


def _stream(content: bytes, *, compress: bool) -> bytes:
    if compress:
        data = zlib.compress(content, 9)
        return (
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
        )
    return b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"


def paged_pdf(pages: int, *, text: str = "Page {n} says hello.") -> bytes:
    """A PDF of `pages` pages, each with one line of text naming its number."""
    # 1 catalog, 2 pages, 3 font, then (page, contents) pairs.
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(pages)).encode()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(pages):
        line = text.format(n=index + 1).encode("latin-1")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * index)
        )
        objects.append(_stream(b"BT /F1 12 Tf 72 720 Td (" + line + b") Tj ET", compress=False))
    return _assemble(objects)


def expanding_pdf(lines: int) -> bytes:
    """One page whose compressed content stream expands to `lines` lines of text.

    The shape the audit used (RAG-02): a few kilobytes on the wire, megabytes of
    text once decompressed.
    """
    operators = (
        b"BT /F1 12 Tf 10 700 Td "
        + b"(AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA) '\n" * lines
        + b"ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        _stream(operators, compress=True),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return _assemble(objects)


#: Characters of extracted text each `expanding_pdf` line produces, measured.
CHARACTERS_PER_EXPANDING_LINE = 46
