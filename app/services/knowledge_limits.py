"""Every resource bound on a knowledge document, in one place.

The audit found one bound where there needed to be six (RAG-02). A 32 KB upload
passed the 400,000-character limit on what was *submitted* and decompressed into
7.5 million characters of text, 7,500 chunks and 79 embedding requests, parsed for
six minutes on the API's event loop. A limit on the input said nothing about the
output, and nothing else was limited at all.

So each stage has its own ceiling, independent of the one before it, because each
earlier one can be defeated in a way the next cannot see:

- `MAX_SUBMITTED_CHARACTERS` - the body: a request too large to read.
- `MAX_PDF_BYTES` - the decoded PDF: base64 that decodes larger than it looks.
- `MAX_PDF_PAGES` - **refused above, never truncated** (RAG-08): a catalogue
  silently indexed to page forty.
- `MAX_EXTRACTED_CHARACTERS`, enforced *during* extraction: a compressed stream
  expanding without end.
- `EXTRACTION_TIMEOUT_SECONDS`, enforced by killing a process: a parser that
  never finishes.
- `MAX_CHUNKS_PER_DOCUMENT`: thousands of embedding inputs.
- `MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT`: the same cost arriving as fewer,
  larger chunks.
- For catalogues and office files (RQ-21): archive entries, inflated bytes and
  compression ratio, rows, columns, cell size and sheets.

The chunk and embedding bounds are checked again by the ingestion worker before
any provider call, not only at submission: a document stored before these limits
existed, or under a looser one, must not be able to spend what a new upload
cannot.

The numbers are derived from one another where they can be, so changing the text
limit moves everything that should move with it.
"""

from __future__ import annotations

from typing import Final

# What a JSON body may carry, as text or as base64. Unchanged from before.
MAX_SUBMITTED_CHARACTERS: Final = 400_000

# Base64 is four characters for every three bytes, so a body at the limit
# decodes to at most this. Checked explicitly anyway: it costs nothing, and it
# keeps the bound true if the submitted limit is ever raised separately.
MAX_PDF_BYTES: Final = MAX_SUBMITTED_CHARACTERS * 3 // 4

# Enough for a long brochure. A longer PDF is refused with this number in the
# message rather than indexed up to it: a document that answers from its first
# forty pages and is silent about the rest looks complete and is not.
MAX_PDF_PAGES: Final = 40

# As much text as could have been pasted directly. A PDF is a container for
# text, not a way to submit more of it than the text endpoint allows.
MAX_EXTRACTED_CHARACTERS: Final = MAX_SUBMITTED_CHARACTERS

# A normal PDF at the page limit parses in a second or two, process start
# included. Twenty is room for a slow host and a busy moment, and short enough
# that an upload request waiting on it still ends inside the proxy's timeout.
# Measured against the audit's compressed PDF: its content stream alone takes
# longer than this to parse, so it is the clock - not the character count -
# that stops it, and the event loop kept ticking every 65 ms throughout.
EXTRACTION_TIMEOUT_SECONDS: Final = 20.0

# How many extractions one process runs at once. Each is a separate process
# with its own memory ceiling; this is what stops a burst of uploads being a
# burst of processes.
MAX_CONCURRENT_EXTRACTIONS: Final = 2

# The memory a parsing process may address, where the platform lets us say so
# (Linux `RLIMIT_AS`). pypdf's own per-stream decompression cap is 75 MB; this
# leaves room for the interpreter and for that cap to be reached.
EXTRACTION_MEMORY_BYTES: Final = 768 * 1024 * 1024

# Measured, not guessed: the chunker produces about 400 chunks from the
# largest text document the submitted limit allows, whatever its shape (audit
# §18). Two and a half times that is headroom for a legitimate document and a
# hard stop well short of the 7,500 the audit reached.
MAX_CHUNKS_PER_DOCUMENT: Final = 1_000

# Chunks overlap, so the characters sent for embedding exceed the document's
# own. One and a half times the extracted limit covers the overlap with room.
MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT: Final = MAX_EXTRACTED_CHARACTERS * 3 // 2

# --- Catalogues and office documents (RQ-21, FX-3.17) -----------------------
#
# An Excel or Word file is a zip of XML, so it has two more ways to be larger
# than it looks: an archive that inflates (a zip bomb) and XML that expands
# (entity declarations). Each has its own bound, checked in the child process
# before any parser reads a part.

# The decoded file, whatever its format: the same ceiling a PDF has, because the
# body that carries it is the same.
MAX_BINARY_DOCUMENT_BYTES: Final = MAX_PDF_BYTES
# Parts in the archive. A real workbook or document has a few dozen.
MAX_ARCHIVE_ENTRIES: Final = 1_000
# Everything the archive inflates to. A 300 KB workbook of text inflates about
# tenfold; this is a hundred times the upload.
MAX_ARCHIVE_UNCOMPRESSED_BYTES: Final = 30 * 1024 * 1024
# Inflated over compressed, per part. Repetitive XML compresses well - a large
# shared-strings part reaches about 40 - and a bomb is in the thousands.
MAX_ARCHIVE_COMPRESSION_RATIO: Final = 200

# One row of a catalogue is one chunk (RQ-21), so the row limit is the chunk
# limit: a 5,000-row catalogue is refused with this number in the message - and
# uploaded as five files of 1,000 - rather than indexed to row 1,000.
MAX_TABLE_ROWS: Final = MAX_CHUNKS_PER_DOCUMENT
# Columns a row may have. A wider sheet is an export of something that is not
# a catalogue, and each row would be a chunk nobody could read.
MAX_TABLE_COLUMNS: Final = 50
# One cell. Longer text belongs in a document, where it is chunked as prose.
MAX_CELL_CHARACTERS: Final = 2_000
# Sheets of a workbook read.
MAX_SHEETS: Final = 20

__all__ = [
    "EXTRACTION_MEMORY_BYTES",
    "EXTRACTION_TIMEOUT_SECONDS",
    "MAX_ARCHIVE_COMPRESSION_RATIO",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_ARCHIVE_UNCOMPRESSED_BYTES",
    "MAX_BINARY_DOCUMENT_BYTES",
    "MAX_CELL_CHARACTERS",
    "MAX_CHUNKS_PER_DOCUMENT",
    "MAX_CONCURRENT_EXTRACTIONS",
    "MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT",
    "MAX_EXTRACTED_CHARACTERS",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_SHEETS",
    "MAX_SUBMITTED_CHARACTERS",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
]
