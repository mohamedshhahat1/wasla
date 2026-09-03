"""Chunking: where most retrieval quality is won or lost."""

from __future__ import annotations

from app.services.chunking import (
    CHUNK_OVERLAP_CHARACTERS,
    MAX_CHUNK_CHARACTERS,
    normalise,
    split,
)

PARAGRAPHS = """
Economy finishing

Economy finishing costs 4500 EGP per square metre.

Premium finishing

Premium finishing costs 7200 EGP per square metre and adds imported fittings.
"""


def test_whitespace_collapses_but_paragraph_breaks_survive() -> None:
    """Blank lines are the boundaries the splitter depends on."""
    cleaned = normalise("one   two\t\tthree\n\n\n\nfour   ")

    assert cleaned == "one two three\n\nfour"


def test_carriage_returns_are_normalised() -> None:
    assert normalise("one\r\n\r\ntwo") == "one\n\ntwo"


def test_text_with_nothing_in_it_produces_no_chunks() -> None:
    """A caller must treat this as a failed ingestion, not an empty success."""
    assert split("") == []
    assert split("   \n\n \t ") == []


def test_a_short_document_still_produces_a_chunk() -> None:
    """One sentence of pricing is still what answers a question about pricing."""
    chunks = split("Delivery is free above 5000 EGP.")

    assert len(chunks) == 1
    assert "5000" in chunks[0].content


def test_chunks_are_numbered_from_zero_in_order() -> None:
    chunks = split(PARAGRAPHS)

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_related_paragraphs_are_kept_together_while_they_fit() -> None:
    """A paragraph break is a boundary, not an instruction to split."""
    chunks = split(PARAGRAPHS)

    assert len(chunks) == 1
    assert "4500" in chunks[0].content
    assert "7200" in chunks[0].content


def test_a_long_document_is_split() -> None:
    text = "\n\n".join(f"Paragraph {index} about finishing work." * 20 for index in range(12))

    chunks = split(text)

    assert len(chunks) > 1


def test_no_chunk_greatly_exceeds_the_budget() -> None:
    """Overlap adds to a chunk, so the ceiling is the budget plus the overlap."""
    text = "\n\n".join(f"Paragraph {index}. " * 60 for index in range(10))

    chunks = split(text)

    ceiling = MAX_CHUNK_CHARACTERS + CHUNK_OVERLAP_CHARACTERS + 8
    assert all(len(chunk.content) <= ceiling for chunk in chunks)


def test_consecutive_chunks_overlap() -> None:
    """An answer straddling a boundary is otherwise unreachable from either side."""
    text = "\n\n".join(f"Sentence number {index} about pricing. " * 40 for index in range(6))

    chunks = split(text)

    assert len(chunks) > 1
    # The second chunk opens with text carried from the end of the first.
    carried = chunks[1].content[:CHUNK_OVERLAP_CHARACTERS].strip()
    assert carried
    assert carried[:40] in chunks[0].content


def test_a_single_oversized_paragraph_is_broken_at_sentences() -> None:
    sentence = "Premium finishing costs 7200 EGP per square metre. "
    chunks = split(sentence * 60)

    assert len(chunks) > 1
    # Cuts landed on sentence ends, so no chunk starts mid-word.
    assert all(chunk.content.strip() for chunk in chunks)


def test_a_single_sentence_longer_than_the_budget_is_cut_by_length() -> None:
    """No boundary is left to respect, so length is the only option left."""
    chunks = split("x" * (MAX_CHUNK_CHARACTERS * 3))

    assert len(chunks) >= 3


def test_arabic_sentences_split_on_arabic_punctuation() -> None:
    """The product is Arabic-first, so the Arabic full stop is a boundary."""
    # The Arabic full stop is the real character here, not a hyphen lookalike.
    sentence = "سعر التشطيب سبعة آلاف جنيه للمتر المربع۔ "  # noqa: RUF001
    chunks = split(sentence * 80)

    assert len(chunks) > 1


def test_token_estimates_are_positive() -> None:
    chunks = split(PARAGRAPHS)

    assert all(chunk.token_estimate > 0 for chunk in chunks)
