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

**Lines and rows are atomic**, and a question with its answer (`Q:`/`A:`, the
Arabic forms, or a numbered question followed by its answer) is **one chunk of
its own** (RQ-21): packed with nothing else, carrying no overlap across its
edges, and never merged into a neighbour however short - a FAQ's question should
match its own answer, not a passage of four answers. A pair written across two
paragraphs (the question, a blank line, the answer) is one pair too.

**A catalogue is chunked a row at a time** (`rows=True`, CSV and XLSX): each
line one chunk, the header already repeated inside it (`Column: value; ...`),
and a `## Sheet` line - a heading, told from a row because it holds no `": "` -
its breadcrumb. A row too long for one chunk is cut into chunks of that row
alone (RR-14).

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
# "1. How long is delivery?", "Q3) ...", "s2. ...?" - a numbered question line.
_NUMBERED_QUESTION: Final = re.compile(
    "^\\s*(?:(?:Q|\u0633)\\s*)?\\d{1,3}\\s*[.):\\-]\\s*\\S.*[?\u061f]\\s*$", re.I
)
# A sheet heading in row mode: a `#` line. A row always holds "Column: value",
# which is how the two are told apart.
_ROW_HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


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
    # The question-and-answer pair this unit belongs to, if any: the units of
    # one pair are one chunk, which no other unit shares (RQ-21).
    pair: int | None = None


def normalise(text: str) -> str:
    """Tidy whitespace without destroying structure (`normalise_whitespace`)."""
    return normalise_whitespace(text)


def split(text: str, *, markdown: bool = False, rows: bool = False) -> list[Chunk]:
    """Split text into overlapping chunks, in document order.

    `markdown` makes `#` lines headings; otherwise a short line standing alone
    between blank lines is one. `rows` chunks a catalogue a line at a time
    (CSV, XLSX). Returns an empty list for text that carries nothing worth
    retrieving, which the caller must treat as a failed ingestion rather than
    an empty success: a document with no chunks is a document that silently
    answers nothing.
    """
    cleaned = normalise(text)
    if not cleaned.strip():
        return []
    if rows:
        return _rows(cleaned)
    units, paths = _units(cleaned, markdown=markdown)
    pieces = _merge_short(cleaned, _pack(cleaned, units))
    return _with_overlap(cleaned, pieces, paths)


# ------------------------------------------------------------------ units


def _units(text: str, *, markdown: bool) -> tuple[list[_Unit], list[str | None]]:
    """The atoms of the text, in order, each tagged with its section and pair."""
    units: list[_Unit] = []
    paths: list[str | None] = [None]
    stack: list[tuple[int, str]] = []
    paragraphs = [
        (paragraph, list(_LINE.finditer(text, paragraph.start(), paragraph.end())))
        for paragraph in _PARAGRAPH.finditer(text)
    ]
    pairs = 0
    index = 0
    while index < len(paragraphs):
        paragraph, lines = paragraphs[index]
        index += 1
        segments = _faq_segments(lines)
        if segments is not None:
            # Several pairs written without blank lines between them - what
            # a PDF's text layer usually gives - are still one pair each.
            for segment, paired in segments:
                if paired:
                    pairs += 1
                    units.extend(
                        _bounded(
                            text, segment[0].start(), segment[-1].end(), len(paths) - 1, pair=pairs
                        )
                    )
                else:
                    for line in segment:
                        units.extend(_bounded(text, line.start(), line.end(), len(paths) - 1))
            continue
        if (
            len(lines) == 1
            and _is_question(lines[0].group())
            and index < len(paragraphs)
            and _ANSWER.match(paragraphs[index][1][0].group())
        ):
            # The question, a blank line, its answer: one pair.
            answer, _ = paragraphs[index]
            index += 1
            pairs += 1
            units.extend(
                _bounded(text, paragraph.start(), answer.end(), len(paths) - 1, pair=pairs)
            )
            continue
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


def _is_question(line: str) -> bool:
    return bool(_QUESTION.match(line) or _NUMBERED_QUESTION.match(line))


def is_question_and_answer(content: str) -> bool:
    """Whether a chunk is one question-and-answer pair (RQ-21): a whole answer.

    What retrieval asks before joining a chunk to its neighbours: a pair's
    neighbour is another question, and joining them is the dilution the pair
    chunk exists to avoid (RR-27). A heading the pair carries is allowed.
    """
    lines = [m for m in _LINE.finditer(content) if m.group().strip()]
    while lines and _MARKDOWN_HEADING.match(lines[0].group().strip()):
        lines = lines[1:]
    segments = _faq_segments(lines)
    return segments is not None and len(segments) == 1 and segments[0][1]


def _faq_segments(
    lines: list[re.Match[str]],
) -> list[tuple[list[re.Match[str]], bool]] | None:
    """A paragraph that opens with a question, cut at every question line.

    Each piece is `(lines, is_pair)`: a `Q:` piece is a pair when it holds an
    `A:` line, a numbered one when an answer line follows the question. A list
    of numbered questions with no answers is pieces of one line, none a pair.
    None when the paragraph does not open with a question.
    """
    if len(lines) < 2:
        return None
    first = lines[0].group()
    lettered = bool(_QUESTION.match(first))
    if not lettered and not _NUMBERED_QUESTION.match(first):
        return None
    starts = _QUESTION if lettered else _NUMBERED_QUESTION
    pieces: list[list[re.Match[str]]] = []
    for line in lines:
        if starts.match(line.group()) or not pieces:
            pieces.append([line])
        else:
            pieces[-1].append(line)
    return [
        (
            piece,
            any(_ANSWER.match(m.group()) for m in piece) if lettered else len(piece) > 1,
        )
        for piece in pieces
    ]


def _tokens(text: str, start: int, end: int) -> int:
    return estimate_tokens(text[start:end])


def _bounded(
    text: str, start: int, end: int, section: int, *, pair: int | None = None
) -> Iterator[_Unit]:
    """One span as units no larger than the budget: whole, sentences, words, cuts."""
    if _tokens(text, start, end) <= MAX_CHUNK_TOKENS:
        yield _Unit(start, end, section, pair=pair)
        return
    for sentence_start, sentence_end in _spans(text, start, end, _SENTENCE_END):
        if _tokens(text, sentence_start, sentence_end) <= MAX_CHUNK_TOKENS:
            yield _Unit(sentence_start, sentence_end, section, pair=pair)
            continue
        for word in _WORD.finditer(text, sentence_start, sentence_end):
            if _tokens(text, word.start(), word.end()) <= MAX_CHUNK_TOKENS:
                yield _Unit(word.start(), word.end(), section, pair=pair)
            else:
                for cut_start, cut_end in _hard_cuts(text, word.start(), word.end()):
                    yield _Unit(cut_start, cut_end, section, pair=pair)


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
        if current and _pair_boundary(current[-1], unit):
            # A question and its answer share a chunk with nothing else; a
            # heading just before the pair goes with it.
            carried_heading: list[_Unit] = []
            while current and current[-1].heading:
                carried_heading.insert(0, current.pop())
            if current:
                pieces.append(current)
            current = carried_heading
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


def _pair_boundary(previous: _Unit, unit: _Unit) -> bool:
    """Whether a chunk ends between two units: either is a pair's, and not the same pair."""
    return (previous.pair is not None or unit.pair is not None) and previous.pair != unit.pair


def _paired(piece: list[_Unit]) -> bool:
    return any(unit.pair is not None for unit in piece)


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
        if (
            merged
            # A short question-and-answer pair is still a whole answer.
            and not _paired(piece)
            and not _paired(merged[-1])
            and (
                _tokens(text, piece[0].start, piece[-1].end) < MIN_CHUNK_TOKENS
                or _tokens(text, merged[-1][0].start, merged[-1][-1].end) < MIN_CHUNK_TOKENS
            )
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
        if (
            previous
            and not piece[0].heading
            and previous[-1].section == piece[0].section
            # Across a pair's edge nothing is carried; inside a long pair, the
            # pair's own words are.
            and previous[-1].pair == piece[0].pair
        ):
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


def _rows(text: str) -> list[Chunk]:
    """A catalogue, one chunk per row, each row's breadcrumb its sheet (RQ-21, RR-14).

    No packing, no overlap and no merging: a row is the fact a customer asks
    about, and a chunk holding two rows ranks the wrong product beside the
    right one. A row longer than one chunk is cut into chunks of that row only.
    """
    chunks: list[Chunk] = []
    path: str | None = None
    for line in _LINE.finditer(text):
        heading = _ROW_HEADING.match(line.group().strip())
        if heading is not None and ": " not in line.group():
            path = heading.group(2)
            continue
        for unit in _bounded(text, line.start(), line.end(), 0):
            body = text[unit.start : unit.end].strip()
            if body:
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        content=body,
                        heading_path=path,
                        start=unit.start,
                        end=unit.end,
                    )
                )
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
    "is_question_and_answer",
    "normalise",
    "split",
]
