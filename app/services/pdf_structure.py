"""What a PDF's page structure leaves in its text, removed; and what it scrambled, flagged.

Runs on the pages the extraction child returned, before anything touches the
database (RQ-09), and only on PDFs - pasted text has no pages.

**Page structure (RQ-05, RV-22).** A text layer is a sequence of pages, and the
furniture of a page is not content: a running header or footer printed on
every page became one passage-worthy line per page, a bare page number became
a chunk of its own, and a paragraph that crossed a page break was split in two
because pages were joined with a blank line. Here a line that sits in the first
or last two lines of at least 60% of the pages (documents of three pages or
more) is dropped from those positions, a page number at a page's edge is
dropped, and two pages are joined with a single newline when the first ends
mid-sentence.

**Re-ordered lines (RQ-04, RV-04).** pypdf's text order for a line mixing
Arabic and digits can differ from the order it is read in: a price glued to the
wrong word (`.4500سعر`), an Arabic-Indic number reversed (`٧٢٠٠` read out as
`٠٠٢٧`). The text cannot be repaired by guessing an order, so it is not: the
lines are counted per warning code, stored on the document and metered, and the
document is still indexed. The original file is what a repair needs.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from app.services.text_normalisation import for_storage

#: Every warning code an extraction can raise, and nothing else.
DIGITS_GLUED_TO_ARABIC: Final = "digits_glued_to_arabic"
ARABIC_INDIC_LEADING_ZERO: Final = "arabic_indic_leading_zero"
EXTRACTION_WARNING_CODES: Final = frozenset({DIGITS_GLUED_TO_ARABIC, ARABIC_INDIC_LEADING_ZERO})

# Applied only to documents long enough for "every page" to mean something.
MIN_PAGES_FOR_RUNNING_LINES: Final = 3
RUNNING_LINE_SHARE: Final = 0.6
EDGE_LINES: Final = 2

_ARABIC_LETTER: Final = "[\u0621-\u064a\u066e-\u06d3\u06fa-\u06ff]"
_GLUED: Final = re.compile(f"[0-9]{_ARABIC_LETTER}|{_ARABIC_LETTER}[0-9]")
# A multi-digit Arabic-Indic (or Persian) number that starts with zero: what a
# reversed `٧٢٠٠` looks like. Leading zeros are rare in prices and quantities.
_INDIC_DIGIT: Final = "[\\u0660-\\u0669\\u06f0-\\u06f9]"
_LEADING_ZERO: Final = re.compile(f"(?<!{_INDIC_DIGIT})[\\u0660\\u06f0]{_INDIC_DIGIT}+")
_DIGITS: Final = "[0-9\\u0660-\\u0669\\u06f0-\\u06f9]+"
# "Page" in English and Arabic (safha, "s."), and "of" ("min"), as regex escapes.
_PAGE_WORD: Final = "page|p\\.|\\u0635\\u0641\\u062d\\u0629|\\u0635\\.?"
_OF_WORD: Final = "/|of|\\u0645\\u0646"
_PAGE_NUMBER: Final = re.compile(
    rf"^[-\u2013\u2014\s]*(?:(?:{_PAGE_WORD})\s*)?{_DIGITS}"
    rf"(?:\s*(?:{_OF_WORD})\s*{_DIGITS})?[-\u2013\u2014\s]*$",
    re.IGNORECASE,
)
_ANY_DIGITS: Final = re.compile(_DIGITS)
# How a sentence (or a list item that is complete) ends.
_TERMINATED: Final = re.compile("[.!?\u061f\u06d4\u061b:;\u2026\"')\\]\u00bb]$")


@dataclass(frozen=True, slots=True)
class StructuredText:
    """A PDF's text with its page furniture removed, and what was flagged."""

    text: str
    warnings: dict[str, int] = field(default_factory=dict)


def _key(line: str) -> str:
    # "Page 3 of 12" and "Page 4 of 12" are the same running line.
    return _ANY_DIGITS.sub("#", line.strip().casefold())


def is_page_number(line: str) -> bool:
    """Whether a line is nothing but a page number, in any of the forms above."""
    return bool(_PAGE_NUMBER.match(line.strip()))


def _running_lines(pages: Sequence[list[str]]) -> set[str]:
    if len(pages) < MIN_PAGES_FOR_RUNNING_LINES:
        return set()
    seen: Counter[str] = Counter()
    for lines in pages:
        edges = {_key(line) for line in lines[:EDGE_LINES] + lines[-EDGE_LINES:] if line.strip()}
        seen.update(edges)
    needed = RUNNING_LINE_SHARE * len(pages)
    return {key for key, count in seen.items() if count >= needed}


def _strip_edges(lines: list[str], running: set[str]) -> list[str]:
    def furniture(line: str) -> bool:
        return not line.strip() or is_page_number(line) or _key(line) in running

    start, end = 0, len(lines)
    while start < end and start < EDGE_LINES * 2 and furniture(lines[start]):
        start += 1
    while end > start and len(lines) - end < EDGE_LINES * 2 and furniture(lines[end - 1]):
        end -= 1
    return lines[start:end]


def warnings_for(text: str) -> dict[str, int]:
    """How many lines of `text` look re-ordered, per warning code."""
    counts: Counter[str] = Counter()
    for line in text.split("\n"):
        if _GLUED.search(line):
            counts[DIGITS_GLUED_TO_ARABIC] += 1
        if _LEADING_ZERO.search(line):
            counts[ARABIC_INDIC_LEADING_ZERO] += 1
    return dict(sorted(counts.items()))


def structure(pages: Sequence[str]) -> StructuredText:
    """One document's text from its pages: furniture removed, pages joined, lines flagged."""
    split = [for_storage(page).split("\n") for page in pages]
    running = _running_lines(split)
    kept = [lines for lines in (_strip_edges(lines, running) for lines in split) if lines]
    text = ""
    for lines in kept:
        page = "\n".join(lines)
        if not text:
            text = page
        elif _TERMINATED.search(text):
            text = f"{text}\n\n{page}"
        else:
            # The paragraph continues on the next page.
            text = f"{text}\n{page}"
    text = for_storage(text)
    return StructuredText(text=text, warnings=warnings_for(text))


__all__ = [
    "ARABIC_INDIC_LEADING_ZERO",
    "DIGITS_GLUED_TO_ARABIC",
    "EXTRACTION_WARNING_CODES",
    "StructuredText",
    "is_page_number",
    "structure",
    "warnings_for",
]
