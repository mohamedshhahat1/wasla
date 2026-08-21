"""Splitting a document into passages worth embedding.

Chunking is where most retrieval quality is won or lost, so the rules here are
deliberate rather than incidental.

Split on structure first, characters second. A paragraph break is a real
boundary an author chose; a fixed character window is not, and cutting mid
sentence produces a chunk that embeds as neither of the two ideas it straddles.
Paragraphs are therefore accumulated up to a size budget and only split by force
when a single paragraph exceeds it on its own.

Chunks overlap. An answer that sits across a boundary is otherwise unreachable
from either side, and the cost of a little duplication is far lower than the
cost of a passage no query can find.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.agents.memory import estimate_tokens

# Characters, not tokens: the split happens before anything is embedded, and a
# character budget needs no tokeniser. The values target roughly 250 tokens per
# chunk for English and rather fewer for Arabic, which is well inside every
# embedding model's input limit while staying specific enough to retrieve.
MAX_CHUNK_CHARACTERS: Final = 1_000
# Carried from the end of one chunk into the start of the next.
CHUNK_OVERLAP_CHARACTERS: Final = 150
# Below this a chunk is not worth a row or an embedding call: a stray heading or
# a page number retrieves noisily and answers nothing.
MIN_CHUNK_CHARACTERS: Final = 40

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_WHITESPACE_RUN = re.compile(r"[ \t]+")
# Sentence-ish boundaries, including the Arabic full stop and question mark,
# since the product is Arabic-first.
# RUF001 reads the Arabic full stop as a lookalike for a hyphen. It is the
# real character, and the product is Arabic-first, so it stays.
_SENTENCE_END = re.compile(r"(?<=[.!?۔؟])\s+")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class Chunk:
    """One passage, ready to embed."""

    ordinal: int
    content: str

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.content)


def normalise(text: str) -> str:
    """Tidy whitespace without destroying structure.

    Runs of spaces and tabs collapse, and trailing space goes, but blank lines
    survive: they are the paragraph boundaries the splitter depends on.
    """
    lines = [
        _WHITESPACE_RUN.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")
    ]
    collapsed = "\n".join(lines)
    # Three or more newlines carry no more meaning than two.
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def split(text: str) -> list[Chunk]:
    """Split text into overlapping chunks, in document order.

    Returns an empty list for text that carries nothing worth retrieving, which
    the caller must treat as a failed ingestion rather than an empty success: a
    document with no chunks is a document that silently answers nothing.
    """
    cleaned = normalise(text)
    if not cleaned:
        return []

    pieces: list[str] = []
    buffer = ""
    for paragraph in _PARAGRAPH_BREAK.split(cleaned):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) > MAX_CHUNK_CHARACTERS:
            # Too big to be a chunk on its own, so flush what is buffered and
            # break this one down rather than letting it swallow the budget.
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_split_long(paragraph))
            continue

        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= MAX_CHUNK_CHARACTERS:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = paragraph

    if buffer:
        pieces.append(buffer)

    return _with_overlap(pieces)


def _split_long(paragraph: str) -> list[str]:
    """Break one oversized paragraph, preferring sentence boundaries."""
    sentences = [part.strip() for part in _SENTENCE_END.split(paragraph) if part.strip()]
    if not sentences:
        return _hard_split(paragraph)

    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > MAX_CHUNK_CHARACTERS:
            # A single sentence longer than the budget: no structural boundary
            # is left to respect, so cut it by length.
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_hard_split(sentence))
            continue

        candidate = f"{buffer} {sentence}" if buffer else sentence
        if len(candidate) <= MAX_CHUNK_CHARACTERS:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = sentence

    if buffer:
        pieces.append(buffer)
    return pieces


def _hard_split(text: str) -> list[str]:
    """Last resort: fixed-width cuts, used only when no boundary exists."""
    return [
        text[start : start + MAX_CHUNK_CHARACTERS]
        for start in range(0, len(text), MAX_CHUNK_CHARACTERS)
    ]


def _with_overlap(pieces: list[str]) -> list[Chunk]:
    """Prefix each chunk with the tail of the one before it.

    The first chunk has nothing to carry, and pieces too short to stand alone
    are dropped rather than embedded, so a page number does not become a
    retrievable passage.
    """
    chunks: list[Chunk] = []
    previous = ""
    for piece in pieces:
        content = piece.strip()
        if not content:
            continue
        if previous:
            tail = previous[-CHUNK_OVERLAP_CHARACTERS:].strip()
            if tail:
                content = f"{tail}\n\n{content}"
        if len(content) >= MIN_CHUNK_CHARACTERS or not chunks:
            # The `not chunks` clause keeps a genuinely short document: one
            # sentence of pricing is still the only thing that answers a
            # question about pricing.
            chunks.append(Chunk(ordinal=len(chunks), content=content))
        previous = piece
    return chunks
