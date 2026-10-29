"""What a chunk is embedded as: the chunk, said where it comes from (RQ-14, FX-3.2).

A chunk reading "costs 7200 EGP per square metre" carries no signal that it is
about premium finishing unless that phrase happens to be inside it. Embedded as

    Finishing price list (U+203A) Pricing > Premium

    costs 7200 EGP per square metre

it lands near the question that names the product. The stored and displayed
chunk is unchanged; only the embedding input carries the label.

**No schema-version bump** (RQ-02): queries are embedded exactly as before, so
a vector built with the label and one built without it are in one space and are
searched together while `reindex-all` rebuilds documents one by one. A labelled
vector is closer to the questions that name its document - which is the point -
and an unlabelled one is what was served before.

A title that says nothing is not a label: an empty one, "untitled", or a bare
filename like `scan_003.pdf` - which is what a document gets when its uploader
gave none. A filename's stem is used when it says something (`price-list.pdf`
-> "price list"); otherwise the label is the section alone, or nothing.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Final

from app.services.chunking import BREADCRUMB_SEPARATOR
from app.services.text_normalisation import for_matching

LABEL_SEPARATOR: Final = " \N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "
_FILENAME: Final = re.compile(
    r"^[^/\\]+\.(?:pdf|txt|md|markdown|csv|tsv|docx?|xlsx?|html?|json|rtf|png|jpe?g)$",
    re.IGNORECASE,
)
_MEANINGLESS: Final = re.compile(
    r"^(?:untitled|document|doc|file|scan|img|image|new|copy|page)?"
    r"(?:[\s_\-.]*(?:document|copy|\(\d+\)))*[\s_\-.]*\d*$",
    re.IGNORECASE,
)


def _says_something(label: str) -> bool:
    return bool(label.strip()) and not _MEANINGLESS.match(label.strip())


def document_label(title: str | None, filename: str | None = None) -> str | None:
    """The document's name as a label, or None when it says nothing."""
    for candidate in (title or "", filename or ""):
        name = candidate.strip()
        if not name:
            continue
        if _FILENAME.match(name):
            name = re.sub(r"[_\-.]+", " ", PurePosixPath(name).stem).strip()
        if _says_something(name):
            return name
    return None


def embedding_input(
    content: str,
    *,
    title: str | None,
    heading_path: str | None,
    filename: str | None = None,
) -> str:
    """The text a chunk is embedded as (RQ-14): its label, a blank line, the chunk."""
    if heading_path:
        # A chunk that opens with its own heading already says it: the
        # breadcrumb keeps only the sections above it, so no word is doubled.
        crumbs = heading_path.split(BREADCRUMB_SEPARATOR)
        if content.lstrip("# ").startswith(crumbs[-1]):
            crumbs = crumbs[:-1]
        heading_path = BREADCRUMB_SEPARATOR.join(crumbs) or None
    parts = [part for part in (document_label(title, filename), heading_path) if part]
    label = LABEL_SEPARATOR.join(parts)
    return for_matching(f"{label}\n\n{content}" if label else content)


__all__ = ["LABEL_SEPARATOR", "document_label", "embedding_input"]
