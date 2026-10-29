"""The ranking rules over a search's candidates (RQ-15, RQ-16, FX-3.4, FX-3.11)."""

from __future__ import annotations

import uuid

import pytest

from app.services.retrieval_ranking import (
    LEXICAL,
    NEAR_DUPLICATE_OVERLAP,
    NONE,
    PARTIAL,
    RRF_K,
    STRONG,
    VECTOR,
    Candidate,
    diversify,
    identifier_tokens,
    join_adjacent,
    keeps,
    lexical_tokens,
    merge_adjacent,
    rrf_fuse,
    tier_for,
    tsquery_text,
    within_margin,
)

GENERATION = uuid.uuid4()
DOCUMENT = uuid.uuid4()


def _candidate(
    name: str,
    *,
    distance: float = 0.5,
    channel: str = VECTOR,
    ordinal: int = 0,
    content: str | None = None,
    document: uuid.UUID | None = None,
    generation: uuid.UUID | None = None,
    tokens: frozenset[str] = frozenset(),
    heading: str | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        document_id=document or DOCUMENT,
        generation_id=generation or GENERATION,
        ordinal=ordinal,
        content=content if content is not None else f"passage {name}",
        title="T",
        heading_path=heading,
        distance=distance,
        channels={channel},
        lexical_tokens=tokens,
    )


# ------------------------------------------------------------------ lexemes


def test_an_identifier_stays_whole_and_its_parts_are_added() -> None:
    tokens = lexical_tokens("Price of BK-35 is 4,500 EGP")
    assert "bk-35" in tokens and "bk" in tokens and "35" in tokens
    # A thousands separator does not split a number (RR-16).
    assert "4500" in tokens and "4" not in tokens and "500" not in tokens


def test_arabic_lexemes_are_folded_like_the_index() -> None:
    digits = "".join(chr(0x0660 + int(d)) for d in "4500")
    price = digits[0] + chr(0x066C) + digits[1:]
    tokens = lexical_tokens(f"بكام موديل BK-35؟ سعر المتر {price} جنيه")
    assert "bk-35" in tokens
    assert "4500" in tokens
    # The definite article is folded off beside the whole word.
    assert "المتر" in tokens and "متر" in tokens


def test_identifiers_are_digit_tokens_and_latin_words_inside_arabic() -> None:
    assert identifier_tokens("بكام موديل BK-35؟ والـ premium") == {"bk-35", "premium"}
    # A Latin word in an English question is just a word.
    assert identifier_tokens("premium finishing price") == frozenset()
    # Parts of an identifier are not identifiers of their own.
    assert "35" not in identifier_tokens("BK-35 price")
    assert identifier_tokens("سعر TV") == frozenset()  # too short to vouch


def test_the_tsquery_quotes_every_lexeme() -> None:
    assert tsquery_text(["it's", "a\\b"]) == "'it''s' | 'a\\\\b'"
    assert tsquery_text([]) is None


# ------------------------------------------------------------------- fusion


def test_fusion_is_by_rank_not_by_raw_score() -> None:
    """M-R47: found by both channels beats first-by-distance in one.

    `alone` is rank 1 of the vector list (1/61); `both` is rank 5 there and
    rank 4 of the lexical list (1/65 + 1/64). RRF puts `both` first; ranking by
    distance, or by any sum of the channels' own scores, puts `alone` first.
    """
    alone = _candidate("alone", distance=0.05)
    both_vector = _candidate("both", distance=0.6)
    both_lexical = _candidate("both", distance=0.6, channel=LEXICAL)
    fillers_v = [_candidate(f"v{n}", distance=0.1 + n / 100) for n in range(3)]
    fillers_l = [_candidate(f"l{n}", distance=0.9, channel=LEXICAL) for n in range(3)]

    fused = rrf_fuse([[alone, *fillers_v, both_vector], [*fillers_l, both_lexical]])

    assert fused[0].chunk_id == both_vector.chunk_id
    assert fused[1].chunk_id == alone.chunk_id
    merged = fused[0]
    assert merged.channels == {VECTOR, LEXICAL}
    assert merged.score == pytest.approx(1 / (RRF_K + 5) + 1 / (RRF_K + 4))


def test_fusion_order_is_deterministic() -> None:
    lists = [[_candidate(f"c{n}", distance=0.5) for n in range(5)]]
    assert [c.chunk_id for c in rrf_fuse(lists)] == [c.chunk_id for c in rrf_fuse(lists)]


# ---------------------------------------------------------------- keep rule


def test_a_far_candidate_is_kept_only_for_a_lexical_identifier_hit() -> None:
    """M-R48: the keep rule admits an identifier hit, not every lexical hit."""
    identifiers = frozenset({"bk-35"})
    hit = _candidate("hit", distance=1.2, channel=LEXICAL, tokens=frozenset({"bk-35", "price"}))
    word = _candidate("word", distance=1.2, channel=LEXICAL, tokens=frozenset({"price"}))
    vector = _candidate("vector", distance=1.2, tokens=frozenset({"bk-35"}))
    close = _candidate("close", distance=0.3)

    assert keeps(hit, threshold=0.75, identifiers=identifiers)
    assert not keeps(word, threshold=0.75, identifiers=identifiers)
    assert not keeps(vector, threshold=0.75, identifiers=identifiers)
    assert keeps(close, threshold=0.75, identifiers=frozenset())


# ---------------------------------------------------------------- diversity


def test_exact_and_near_repeats_are_dropped() -> None:
    first = _candidate(
        "a", content="Premium finishing costs 7900 EGP per metre, materials included."
    )
    repeat = _candidate(
        "b",
        content="Premium finishing costs 7900 EGP per metre, materials included.",
        document=uuid.uuid4(),
    )
    near = _candidate(
        "c",
        content="Premium finishing costs 7900 EGP per metre, materials included today.",
        document=uuid.uuid4(),
        ordinal=5,
    )
    other = _candidate("d", content="Shipping to Alexandria takes two days.", ordinal=9)

    result = diversify([first, repeat, near, other])

    assert [c.chunk_id for c in result.kept] == [first.chunk_id, other.chunk_id]
    assert result.duplicates == 2
    assert NEAR_DUPLICATE_OVERLAP == 0.8


def test_neighbours_sharing_their_overlap_are_not_repeats() -> None:
    first = _candidate("a", content="one two three four five six seven", ordinal=3)
    second = _candidate("b", content="five six seven eight nine", ordinal=4)
    assert len(diversify([first, second]).kept) == 2


def test_one_document_holds_at_most_three_passages() -> None:
    """M-R49: the per-document cap."""
    many = [
        _candidate(f"m{n}", content=f"distinct passage number {n} about zebra{n}", ordinal=n * 3)
        for n in range(5)
    ]
    other = _candidate("o", content="another document entirely", document=uuid.uuid4())

    result = diversify([*many, other])

    assert len([c for c in result.kept if c.document_id == DOCUMENT]) == 3
    assert other.chunk_id in {c.chunk_id for c in result.kept}
    assert result.capped == 2


# ------------------------------------------------------------------- merging


def test_adjacent_chunks_join_without_their_overlap() -> None:
    assert (
        join_adjacent("alpha beta gamma. delta epsilon", "delta epsilon zeta eta")
        == "alpha beta gamma. delta epsilon zeta eta"
    )
    # No overlap at a word boundary: joined on a new line, nothing lost.
    assert join_adjacent("alpha beta", "gamma delta") == "alpha beta\ngamma delta"
    # Part of a word is not an overlap.
    assert join_adjacent("price 79", "9000 EGP") == "price 79\n9000 EGP"


def test_neighbours_of_one_section_merge_in_document_order() -> None:
    second = _candidate("s", content="rows 11 to 20", ordinal=4, distance=0.2, heading="Prices")
    first = _candidate("f", content="rows 1 to 10", ordinal=3, distance=0.4, heading="Prices")
    across = _candidate("x", content="next section", ordinal=5, heading="Terms")

    merged = merge_adjacent([second, first, across])

    assert len(merged) == 2
    assert merged[0].content == "rows 1 to 10\nrows 11 to 20"
    assert merged[0].ordinals == (3, 4)
    assert merged[0].distance == 0.2
    assert merged[1].ordinals == (5,)


# -------------------------------------------------------- tiers and margin


def test_tiers_follow_the_calibrated_cut_offs() -> None:
    assert tier_for(0.2, strong=0.3, partial=0.5) == STRONG
    assert tier_for(0.4, strong=0.3, partial=0.5) == PARTIAL
    assert tier_for(0.6, strong=0.3, partial=0.5) == NONE
    assert tier_for(None, strong=0.3, partial=0.5) == NONE


def test_the_margin_is_inert_until_set() -> None:
    candidates = [_candidate("a", distance=0.1), _candidate("b", distance=0.9)]
    assert within_margin(candidates, margin=None) == candidates
    assert [c.distance for c in within_margin(candidates, margin=0.2)] == [0.1]
