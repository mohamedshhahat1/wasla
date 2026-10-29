"""The structure-aware chunker (FX-1.2, RV-05, RV-06, RV-08, RV-09, P-R01, P-R02).

The properties are checked over generated text and over every document of the
retrieval evaluation corpus, and the review's probes are kept as examples.
"""

from __future__ import annotations

import itertools
import random
import re
from pathlib import Path

import pytest

import app.services.chunking as chunking_module
from app.agents.memory import estimate_tokens
from app.services.chunking import (
    CHUNK_OVERLAP_TOKENS,
    MAX_CHUNK_TOKENS,
    MIN_CHUNK_TOKENS,
    Chunk,
    normalise,
    split,
)
from app.services.text_normalisation import for_storage

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "eval" / "corpus"
NUMBER = re.compile(r"\d[\d,.]*\d|\d")
ARABIC_WORDS = (
    "نقدم",
    "خدمات",
    "الدعم",
    "الفني",
    "مدار",
    "الساعة",
)


def test_the_chunker_under_test_is_this_worktrees() -> None:
    assert (
        Path(chunking_module.__file__).resolve().parents[2] == Path(__file__).resolve().parents[2]
    )


def _generated(seed: int) -> str:
    generator = random.Random(seed)  # noqa: S311 - reproducible samples
    blocks = []
    for _ in range(generator.randint(3, 12)):
        kind = generator.choice(["heading", "list", "prose", "arabic", "run"])
        if kind == "heading":
            blocks.append(f"# Section {generator.randint(1, 99)}")
        elif kind == "list":
            blocks.append(
                "\n".join(
                    f"Item {n} - price {generator.randint(1, 99)},{generator.randint(100, 999)} EGP"
                    for n in range(generator.randint(2, 60))
                )
            )
        elif kind == "prose":
            blocks.append(
                " ".join(f"Clause {n} covers delivery." for n in range(generator.randint(1, 80)))
            )
        elif kind == "arabic":
            blocks.append(
                " ".join(generator.choice(ARABIC_WORDS) for _ in range(generator.randint(5, 600)))
            )
        else:
            blocks.append(" ".join(str(generator.randint(0, 99999)) for _ in range(400)))
    return "\n\n".join(blocks)


def _documents() -> list[tuple[str, str, bool]]:
    found = [
        (path.name, for_storage(path.read_text(encoding="utf-8")), path.suffix == ".md")
        for path in sorted(CORPUS.iterdir())
    ]
    return found + [(f"generated-{seed}", _generated(seed), True) for seed in range(40)]


DOCUMENTS = _documents()


def _check_properties(text: str, chunks: list[Chunk]) -> None:
    source = normalise(text)
    words = set(source.split())
    assert chunks
    position = 0
    for chunk in chunks:
        # Pieces are exact, consecutive slices; only whitespace lies between.
        assert source[position : chunk.start].strip() == ""
        piece = source[chunk.start : chunk.end]
        assert chunk.content.endswith(piece)
        position = chunk.end
        # No boundary and no overlap start inside a word or a number.
        assert set(chunk.content.split()) <= words
        overlap = chunk.content[: -len(piece)]
        if overlap:
            # Joined with one newline, never a blank line, and never only overlap.
            assert overlap.endswith("\n") and not overlap.endswith("\n\n")
            assert piece.strip()
        assert chunk.token_estimate <= MAX_CHUNK_TOKENS + CHUNK_OVERLAP_TOKENS + MIN_CHUNK_TOKENS
    assert source[position:].strip() == ""
    for number in set(NUMBER.findall(source)):
        assert any(number in chunk.content for chunk in chunks)


@pytest.mark.parametrize(("name", "text", "markdown"), DOCUMENTS, ids=[d[0] for d in DOCUMENTS])
def test_every_chunker_property_holds(name: str, text: str, markdown: bool) -> None:
    _check_properties(text, split(text, markdown=markdown))


def test_whitespace_collapses_but_paragraph_breaks_survive() -> None:
    assert normalise("a  b\t c\n\n\n\nd") == "a b c\n\nd"


def test_text_with_nothing_in_it_produces_no_chunks() -> None:
    assert split("") == []
    assert split("  \n\n \t ") == []


def test_a_short_document_still_produces_a_chunk() -> None:
    (chunk,) = split("Premium finishing costs 7200 EGP.")
    assert chunk.content == "Premium finishing costs 7200 EGP."


def test_a_price_list_without_full_stops_keeps_every_price_whole() -> None:
    # RAG_SYSTEM_REVIEW.md A.2: cut at "price 13" | ",795 EGP" and glued "\n\n".
    lines = "\n".join(
        f"Bookshelf model {n} - price {13 + n},{795 + n} EGP - warranty 3 years" for n in range(40)
    )
    chunks = split(lines)
    assert len(chunks) > 1
    for chunk in chunks:
        assert "\n\n" not in chunk.content
        for line in chunk.content.split("\n"):
            assert re.fullmatch(r"Bookshelf model \d+ - price \d+,\d+ EGP - warranty 3 years", line)


def test_arabic_prose_without_full_stops_is_never_cut_inside_a_word() -> None:
    text = " ".join(ARABIC_WORDS * 120)
    chunks = split(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert set(chunk.content.split()) <= set(ARABIC_WORDS)


def test_a_page_number_never_becomes_a_passage() -> None:
    # A.1 case 1: "Page 3" became a chunk of overlap plus "Page 3".
    long = " ".join(["Refunds are accepted within fourteen days of delivery."] * 20)
    chunks = split(f"{long}\n\nPage 3\n\n{long}")
    assert not [c for c in chunks if c.content.strip().endswith("Page 3")]
    assert all(chunk.heading_path is None for chunk in chunks)


def test_a_heading_moves_with_the_section_it_introduces() -> None:
    # A.1 case 2: "## Refund policy" ended the Shipping chunk on its own.
    policy = " ".join(
        ["Customers may return any unused item. The item must be in its original packaging."] * 14
    )
    text = f"## Shipping\n\nWe ship within three days.\n\n## Refund policy\n\n{policy}"
    chunks = split(text, markdown=True)
    assert not [c for c in chunks if c.content.rstrip().endswith("## Refund policy")]
    for chunk in chunks:
        if "Customers may" in chunk.content and "We ship" not in chunk.content:
            assert chunk.heading_path == "Refund policy"


def test_markdown_headings_are_structure_with_breadcrumbs() -> None:
    body = " ".join(["Premium finishing uses imported materials and a certified crew."] * 30)
    text = f"# Pricing\n\n## Economy\n\n4500 EGP per square metre.\n\n## Premium\n\n{body}"
    chunks = split(text, markdown=True)
    premium = [c for c in chunks if "imported materials" in c.content]
    assert len(premium) > 1
    assert {c.heading_path for c in premium} == {"Pricing > Premium"}
    # Not markdown: the same "#" lines are plain short lines, still sections.
    assert all(c.heading_path for c in split(text))


def test_overlap_is_whole_sentences_or_words() -> None:
    # A.1 case 6: chunk 1 began "o foxtrot golf.".
    text = "alpha bravo charlie delta echo foxtrot golf. " * 60
    chunks = split(text)
    assert len(chunks) > 1
    assert chunks[1].content.startswith("alpha bravo")


def test_a_question_and_its_answer_stay_together() -> None:
    pairs = "\n\n".join(
        f"Q: Can I reschedule slot {n}?\nA: Yes, slot {n} moves up to {n} hours before."
        for n in range(30)
    )
    for chunk in split(pairs):
        for match in re.finditer(r"Q: Can I reschedule slot (\d+)\?", chunk.content):
            assert f"A: Yes, slot {match.group(1)} moves" in chunk.content


@pytest.mark.parametrize("lead", [1_700, 2_000, 2_300])
def test_a_single_unbroken_token_is_cut_but_never_inside_a_digit_run(lead: int) -> None:
    # The digit run straddles where a cut by length would fall for some leads:
    # at 2,000 the cut happens to land on its edge, which proves nothing.
    token = "x" * lead + "1234567890" * 50 + "y" * 500
    pieces = [c.content.split("\n")[-1] for c in split(token)]
    assert len(pieces) >= 3
    for left, right in itertools.pairwise(pieces):
        assert not (left[-1].isdigit() and right[0].isdigit())


@pytest.mark.parametrize("sentences", range(1, 70, 3))
@pytest.mark.parametrize("markdown", [False, True])
def test_no_piece_shorter_than_the_minimum_stands_alone(sentences: int, markdown: bool) -> None:
    # RV-08: the minimum applies to the piece, never to overlap plus piece. A
    # short section before a heading whose section will not fit is cut off on
    # its own by packing; only the minimum folds it into its neighbour.
    long = " ".join(["Refunds are accepted within fourteen days of delivery."] * sentences)
    mark = "# " if markdown else ""
    text = normalise(f"{mark}Intro\n\nHi there.\n\n{mark}Policy\n\n{long}\n\nThanks!")
    chunks = split(text, markdown=markdown)
    if len(chunks) > 1:
        for chunk in chunks:
            assert estimate_tokens(text[chunk.start : chunk.end]) >= MIN_CHUNK_TOKENS


def test_token_estimates_are_positive_and_sizes_comparable_across_scripts() -> None:
    english = split(" ".join(["Premium finishing costs 7200 EGP per metre."] * 200))
    arabic = split(" ".join(ARABIC_WORDS * 300))
    assert all(chunk.token_estimate > 0 for chunk in english + arabic)
    biggest = max(c.token_estimate for c in english + arabic)
    assert biggest <= MAX_CHUNK_TOKENS + CHUNK_OVERLAP_TOKENS
