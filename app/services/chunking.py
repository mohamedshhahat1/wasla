"""Splitting a document into passages worth embedding.

Chunking is where most retrieval quality is won or lost, so the rules here are
deliberate rather than incidental (FX-1.2, IMP-04).

**Split on structure, in priority order.** A heading starts a section; a blank
line ends a paragraph; a line ends a list item or a table row; then a sentence;
then a word. A cut by length is the last resort, and it never lands inside a
run of digits. The previous chunker knew only blank lines and full stops and
cut everything else every 1,000 characters, so a price list without full stops
came out as `price 13` | `,795 EGP`, and Arabic prose was cut inside a word
(RV-05).

**Lines and rows are atomic**, and a question with its answer (`Q:`/`A:`, and
the Arabic forms) is one unit: a catalogue's row is the fact a customer asks
about.

**Size is measured in estimated tokens**, not characters (`estimate_tokens`),
so an Arabic chunk and an English chunk hold comparable amounts of meaning and
cost comparable amounts to embed.

**Every chunk knows its section.** `heading_path` - `"Pricing > Premium"` - is
the headings it sits under, outermost first (RV-09: a heading before a long
section used to end the previous chunk on its own, and the section's later
chunks carried no heading at all). Markdown headings are `#` lines; in plain
text and PDFs a short line standing alone between blank lines is one.

**Chunks overlap by whole units**, the trailing lines, sentences or words of
the chunk before, joined with one newline (RV-06: the old overlap was the last
150 *characters*, glued on with a blank line, so every chunk began mid-word).
Overlap never crosses into a new section: a section starts clean.

**The minimum length applies to the piece, not to the piece plus its overlap**
(RV-08): a stray `Page 3` used to become a passage of overlap plus "Page 3". A
piece too short to stand alone is merged into a neighbour.

Pure: no I/O. The pieces, without their overlap, are exact consecutive slices
of the text, so nothing is lost and nothing is invented - which the tests check
as a property.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from app.agents.memory import estimate_tokens
from app.services.pdf_structure import is_page_number
from app.services.text_normalisation import normalise_whitespace

# Estimated tokens. About 1,000 characters of English or 500 of Arabic: well
# inside every embedding model's input, specific enough to retrieve.
MAX_CHUNK_TOKENS: Final = 250
# Carried from the end of one chunk into the start of the next: about a
# sentence. Whole units only, so it may carry less, never part of a word.
CHUNK_OVERLAP_TOKENS: Final = 40
# Below this a piece is not worth a row and an embedding call of its own - a
# stray heading or page number retrieves noisily and answers nothing - so it is
# merged into a neighbour instead.
MIN_CHUNK_TOKENS: Final = 10
# A heading is a short line; anything longer is a sentence that happens to
# stand alone.
MAX_HEADING_CHARACTERS: Final = 80
# The most a chunk's piece can hold in characters (all ASCII at the budget).
MAX_CHUNK_CHARACTERS: Final = MAX_CHUNK_TOKENS * 4
BREADCRUMB_SEPARATOR: Final = " > "

_PARAGRAPH: Final = re.compile(r"[^\n](?:.|\n(?!\s*\n))*")
_LINE: Final = re.compile(r"[^\n]+")
_MARKDOWN_HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WORD: Final = re.compile(r"\S+")
_DIGIT: Final = re.compile(r"\d")
_LIST_OR_ROW: Final = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)]|\|)")
# Sentence ends, with the Arabic question mark, full stop and semicolon.
_STOPS: Final = ".!?\u061f\u06d4\u061b"
_SENTENCE_END: Final = re.compile(f"(?<=[{re.escape(_STOPS)}])\\s+")
_TERMINATED: Final = re.compile(f"[{re.escape(_STOPS)}:;,\u060c]$")
# "Q:" / "A:", and the Arabic "s:" / "j:" (sual / jawab) a FAQ is written with.
_QUESTION: Final = re.compile("^\\s*(?:Q|Question|\u0633|\u0633\u0624\u0627\u0644)\\s*:", re.I)
_ANSWER: Final = re.compile("^\\s*(?:A|Answer|\u062c|\u062c\u0648\u0627\u0628)\\s*:", re.I)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One passage, ready to embed.

    `start` and `end` locate the piece - the chunk without its overlap - in the
    normalised text it was cut from.
    """

    ordinal: int
    content: str
    heading_path: str | None = None
    start: int = 0
    end: int = 0

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)


@dataclass(frozen=True, slots=True)
class _Unit:
    start: int
    end: int
    section: int
    heading: bool = False


def normalise(text: str) -> str:
    """Tidy whitespace without destroying structure (`normalise_whitespace`)."""
    return normalise_whitespace(text)


def split(text: str, *, markdown: bool = False) -> list[Chunk]:
    """Split text into overlapping chunks, in document order.

    `markdown` makes `#` lines headings; otherwise a short line standing alone
    between blank lines is one. Returns an empty list for text that carries
    nothing worth retrieving, which the caller must treat as a failed ingestion
    rather than an empty success: a document with no chunks is a document that
    silently answers nothing.
    """
    cleaned = normalise(text)
    if not cleaned.strip():
        return []
    units, paths = _units(cleaned, markdown=markdown)
    pieces = _merge_short(cleaned, _pack(cleaned, units))
    return _with_overlap(cleaned, pieces, paths)


# ------------------------------------------------------------------ units


def _units(text: str, *, markdown: bool) -> tuple[list[_Unit], list[str | None]]:
    """The atoms of the text, in order, each tagged with its section."""
    units: list[_Unit] = []
    paths: list[str | None] = [None]
    stack: list[tuple[int, str]] = []
    for paragraph in _PARAGRAPH.finditer(text):
        lines = list(_LINE.finditer(text, paragraph.start(), paragraph.end()))
        heading = _heading(lines, markdown=markdown)
        if heading is not None:
            level, title = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            paths.append(BREADCRUMB_SEPARATOR.join(name for _, name in stack))
            units.append(_Unit(lines[0].start(), lines[0].end(), len(paths) - 1, heading=True))
            continue
        section = len(paths) - 1
        if _is_question_and_answer(lines):
            units.extend(_bounded(text, paragraph.start(), paragraph.end(), section))
            continue
        for line in lines:
            units.extend(_bounded(text, line.start(), line.end(), section))
    return units, paths


def _heading(lines: list[re.Match[str]], *, markdown: bool) -> tuple[int, str] | None:
    if len(lines) != 1:
        return None
    line = lines[0].group().strip()
    if markdown:
        found = _MARKDOWN_HEADING.match(line)
        return (len(found.group(1)), found.group(2)) if found else None
    if (
        len(line) > MAX_HEADING_CHARACTERS
        or _TERMINATED.search(line)
        or _LIST_OR_ROW.match(line)
        or not any(character.isalpha() for character in line)
        or is_page_number(line)
    ):
        return None
    return 1, line


def _is_question_and_answer(lines: list[re.Match[str]]) -> bool:
    return (
        len(lines) >= 2
        and bool(_QUESTION.match(lines[0].group()))
        and any(_ANSWER.match(line.group()) for line in lines[1:])
    )


def _tokens(text: str, start: int, end: int) -> int:
    return estimate_tokens(text[start:end])


def _bounded(text: str, start: int, end: int, section: int) -> Iterator[_Unit]:
    """One span as units no larger than the budget: whole, sentences, words, cuts."""
    if _tokens(text, start, end) <= MAX_CHUNK_TOKENS:
        yield _Unit(start, end, section)
        return
    for sentence_start, sentence_end in _spans(text, start, end, _SENTENCE_END):
        if _tokens(text, sentence_start, sentence_end) <= MAX_CHUNK_TOKENS:
            yield _Unit(sentence_start, sentence_end, section)
            continue
        for word in _WORD.finditer(text, sentence_start, sentence_end):
            if _tokens(text, word.start(), word.end()) <= MAX_CHUNK_TOKENS:
                yield _Unit(word.start(), word.end(), section)
            else:
                for cut_start, cut_end in _hard_cuts(text, word.start(), word.end()):
                    yield _Unit(cut_start, cut_end, section)


def _spans(text: str, start: int, end: int, boundary: re.Pattern[str]) -> Iterator[tuple[int, int]]:
    position = start
    for found in boundary.finditer(text, start, end):
        if found.start() > position:
            yield position, found.start()
        position = found.end()
    if position < end:
        yield position, end


def _hard_cuts(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """The last resort for one unbroken token: cut by length, never inside digits."""
    position = start
    while position < end:
        cut = position + 1
        while cut < end and _tokens(text, position, cut + 1) <= MAX_CHUNK_TOKENS:
            cut += 1
        back = cut
        while (
            position + 1 < back < end and _DIGIT.match(text[back - 1]) and _DIGIT.match(text[back])
        ):
            # Back off to the start of the digit run rather than split a number.
            back -= 1
        if back > position + 1:
            cut = back
        yield position, cut
        position = cut


# ------------------------------------------------------------------ packing


def _pack(text: str, units: list[_Unit]) -> list[list[_Unit]]:
    """Consecutive units grouped while they fit; a heading moves with its body."""
    pieces: list[list[_Unit]] = []
    current: list[_Unit] = []
    ends = _section_ends(units)
    for index, unit in enumerate(units):
        if (
            unit.heading
            and current
            and not current[-1].heading
            and _tokens(text, current[0].start, ends[index]) > MAX_CHUNK_TOKENS
        ):
            # A section that will not fit where it starts begins a chunk of its
            # own, so every chunk of it carries its own breadcrumb.
            pieces.append(current)
            current = []
        if current and _tokens(text, current[0].start, unit.end) > MAX_CHUNK_TOKENS:
            # A heading is never the last thing in a piece (RV-09).
            carried: list[_Unit] = []
            while current and current[-1].heading:
                carried.insert(0, current.pop())
            if current:
                pieces.append(current)
            current = carried
            if current and _tokens(text, current[0].start, unit.end) > MAX_CHUNK_TOKENS:
                pieces.append(current)
                current = []
        current.append(unit)
    if current:
        pieces.append(current)
    return pieces


def _section_ends(units: list[_Unit]) -> list[int]:
    """For each unit, where the section it belongs to ends in the text."""
    ends = [0] * len(units)
    end = units[-1].end if units else 0
    for index in range(len(units) - 1, -1, -1):
        ends[index] = end
        if units[index].heading:
            end = units[index - 1].end if index else 0
    return ends


def _merge_short(text: str, pieces: list[list[_Unit]]) -> list[list[_Unit]]:
    """Fold a piece too short to stand alone into a neighbour (RV-08)."""
    merged: list[list[_Unit]] = []
    for piece in pieces:
        if merged and (
            _tokens(text, piece[0].start, piece[-1].end) < MIN_CHUNK_TOKENS
            or _tokens(text, merged[-1][0].start, merged[-1][-1].end) < MIN_CHUNK_TOKENS
        ):
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    return merged


def _with_overlap(text: str, pieces: list[list[_Unit]], paths: list[str | None]) -> list[Chunk]:
    chunks: list[Chunk] = []
    previous: list[_Unit] = []
    for piece in pieces:
        start, end = piece[0].start, piece[-1].end
        body = text[start:end]
        first_body = next((unit for unit in piece if not unit.heading), piece[0])
        overlap = ""
        if previous and not piece[0].heading and previous[-1].section == piece[0].section:
            overlap = _overlap(text, previous)
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                content=f"{overlap}\n{body}" if overlap else body,
                heading_path=paths[first_body.section],
                start=start,
                end=end,
            )
        )
        previous = piece
    return chunks


def _overlap(text: str, piece: list[_Unit]) -> str:
    """The trailing whole units of a piece - or its last words - within the budget."""
    end = piece[-1].end
    taken: list[_Unit] = []
    for unit in reversed(piece):
        if unit.heading or _tokens(text, unit.start, end) > CHUNK_OVERLAP_TOKENS:
            break
        taken.insert(0, unit)
    if taken:
        return text[taken[0].start : end].strip()
    words = list(_WORD.finditer(text, piece[-1].start, end))
    kept = 0
    while (
        kept < len(words) and _tokens(text, words[-kept - 1].start(), end) <= CHUNK_OVERLAP_TOKENS
    ):
        kept += 1
    return text[words[-kept].start() : end].strip() if kept else ""


__all__ = [
    "BREADCRUMB_SEPARATOR",
    "CHUNK_OVERLAP_TOKENS",
    "MAX_CHUNK_CHARACTERS",
    "MAX_CHUNK_TOKENS",
    "MIN_CHUNK_TOKENS",
    "Chunk",
    "normalise",
    "split",
]
