"""The process a knowledge PDF is parsed in, and nothing else.

Run by `app.services.extraction.extract_pdf_bounded` as a separate interpreter,
with the PDF on stdin and one JSON object on stdout. It exists as a *process*
rather than a thread because a thread cannot be stopped: a timed-out `pypdf` call
in a thread keeps burning a core after its caller has given up, and the audit's
compressed PDF did that for six minutes (RAG-02). A process can be killed.

**Deliberately standalone.** It imports the standard library and `pypdf` and
nothing from `app`, so it never loads settings, never opens a connection and is
started with an environment that holds no secrets - the file it is reading was
written by somebody else.

Bounds it enforces itself, before the parent's wall clock has to:

* more pages than allowed is refused *before* any page is read (RAG-08);
* extracted text is counted as the parser produces it, and extraction stops the
  moment the count passes the limit rather than finishing and truncating;
* where the platform supports it, address space and CPU time are capped, so a
  decompression bomb ends as a failed process rather than a swapping host.

The parent treats anything other than a well-formed answer - no output, a crash,
a non-zero exit, a kill - as a document that could not be read.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
from typing import Any


class _TooMuchTextError(Exception):
    """Raised from inside the parser's text callback to stop it early."""


def _limit_resources(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        # POSIX only. Imported by name so the module type-checks on every
        # platform; on Windows the parent's kill is the only bound.
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
    # NUL cannot be stored in PostgreSQL text and a lone surrogate cannot be
    # encoded; neither is meaningful text a PDF was trying to say.
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def extract(data: bytes, *, max_pages: int, max_characters: int) -> dict[str, Any]:
    """The answer for one PDF, as the dictionary the parent decodes."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty user password opens most "protected" brochures; anything
            # else is a document this cannot read and must not guess at.
            try:
                reader.decrypt("")
            except Exception:
                return {"ok": False, "code": "unreadable"}
        pages = len(reader.pages)
        if pages > max_pages:
            return {"ok": False, "code": "too_many_pages", "pages": pages}

        produced = 0

        def count(text: Any, *_args: Any) -> None:
            nonlocal produced
            if isinstance(text, str):
                produced += len(text)
            if produced > max_characters:
                raise _TooMuchTextError

        parts: list[str] = []
        total = 0
        for page in reader.pages:
            part = (page.extract_text(visitor_text=count) or "").strip()
            if not part:
                continue
            total += len(part) + 2
            if total > max_characters:
                raise _TooMuchTextError
            parts.append(part)
    except _TooMuchTextError:
        return {"ok": False, "code": "too_much_text", "pages": 0}
    except (PyPdfError, ValueError, OSError, RecursionError, KeyError, TypeError):
        return {"ok": False, "code": "unreadable"}
    except MemoryError:
        return {"ok": False, "code": "too_much_text", "pages": 0}

    return {"ok": True, "text": _clean("\n\n".join(parts).strip()), "pages": pages}


def main(argv: list[str]) -> int:
    max_pages, max_characters, max_bytes, memory_bytes, cpu_seconds = (int(v) for v in argv[1:6])
    _limit_resources(memory_bytes, cpu_seconds)
    data = sys.stdin.buffer.read(max_bytes + 1)
    if len(data) > max_bytes:
        _emit({"ok": False, "code": "too_many_bytes"})
        return 0
    _emit(extract(data, max_pages=max_pages, max_characters=max_characters))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
