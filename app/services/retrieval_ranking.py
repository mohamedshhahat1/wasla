"""How a search's candidates become the passages a model reads (RC-4, RQ-15, RQ-16).

Pure functions over what the repository returned, so every rule here is tested
without a database and none of them can widen what the repository's tenant,
generation, space, scope and validity predicates already narrowed: they only
reorder, merge or drop rows they are handed.

- **Lexemes** (`lexical_tokens`) are what the exact-word channel indexes and
  asks for. Tokenised here rather than by PostgreSQL's `'simple'` parser, which
  splits `BK-35` into `bk` and `-35` and `4,500` into `4` and `500` (RR-16): an
  identifier stays whole, and its parts are added beside it.
- **Fusion** is Reciprocal Rank Fusion: a candidate scores 1/(k + rank) in each
  list it appears in. Ranks, not scores, because a cosine distance and a text
  rank are not on one scale and a weighted sum of them is a guess.
- **The keep rule**: a candidate is kept if it is close enough by distance, or
  if it is an exact-word hit on an identifier of the query - a token holding a
  digit, or a Latin-script word inside an Arabic question - which is what an
  embedding is worst at and what a customer types when they know what they want.
- **Diversity**: an exact or near repeat (80 % of the shorter one's tokens
  shared) is dropped, adjacent chunks of one generation are merged with their
  overlap removed, and one document may hold at most a few passages.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

from app.services.text_normalisation import for_lexical, for_matching

RRF_K: Final = 60
# Candidates each channel contributes before fusion (RQ-15).
CHANNEL_CANDIDATES: Final = 20
NEAR_DUPLICATE_OVERLAP: Final = 0.8
# Words per run the near-repeat test compares (see `shingles`).
SHINGLE: Final = 3
MAX_PASSAGES_PER_DOCUMENT: Final = 3
# The fewest characters a Latin word inside an Arabic question needs to count
# as an identifier - "BK", "TV" are too common to vouch for a passage alone.
MIN_LATIN_IDENTIFIER: Final = 3

VECTOR: Final = "vector"
LEXICAL: Final = "lexical"

STRONG: Final = "strong"
PARTIAL: Final = "partial"
NONE: Final = "none"

_THOUSANDS: Final = re.compile("(?<=\\d)[,\u066c](?=\\d{3}(?!\\d))")
_TOKEN: Final = re.compile(r"\w+(?:[-./]\w+)*")
_PARTS: Final = re.compile(r"[-./]")
_DIGIT: Final = re.compile(r"\d")
_LATIN: Final = re.compile(r"[a-z]")
_ARABIC: Final = re.compile("[\u0600-\u06ff]")
# Arabic prefixes folded off a word for matching: "وال" (and the), "بال",
# "لل", "ال". Light folding, not stemming: 'simple' has none for Arabic.
_ARABIC_PREFIXES: Final = (
    "\u0648\u0627\u0644",
    "\u0628\u0627\u0644",
    "\u0644\u0644",
    "\u0627\u0644",
)


def lexical_tokens(text: str) -> list[str]:
    """The lexemes a text is indexed or queried by, in order of appearance, once each."""
    folded = _THOUSANDS.sub("", for_lexical(text))
    seen: dict[str, None] = {}
    for match in _TOKEN.finditer(folded):
        token = match.group(0).strip("_")
        if not token:
            continue
        seen.setdefault(token, None)
        if _PARTS.search(token):
            for part in _PARTS.split(token):
                if part:
                    seen.setdefault(part, None)
        if _ARABIC.match(token):
            for prefix in _ARABIC_PREFIXES:
                if token.startswith(prefix) and len(token) - len(prefix) >= 2:
                    seen.setdefault(token[len(prefix) :], None)
                    break
    return list(seen)


def identifier_tokens(query: str) -> frozenset[str]:
    """The query's tokens that identify something exactly (RQ-15's keep rule)."""
    folded = _THOUSANDS.sub("", for_lexical(query))
    arabic = bool(_ARABIC.search(folded))
    found = set()
    # Whole tokens as typed: the parts of `bk-35` are indexed beside it, but a
    # bare `35` is not something the customer named.
    for match in _TOKEN.finditer(folded):
        token = match.group(0).strip("_")
        if _DIGIT.search(token) or (
            arabic and _LATIN.match(token) and len(token) >= MIN_LATIN_IDENTIFIER
        ):
            found.add(token)
    return frozenset(found)


def tsquery_text(tokens: Iterable[str]) -> str | None:
    """An OR of quoted lexemes, as `CAST(:q AS tsquery)` reads it; None for no tokens."""
    quoted = [
        "'" + token.replace("\\", "\\\\").replace("'", "''") + "'" for token in tokens if token
    ]
    return " | ".join(quoted) if quoted else None


@dataclass(slots=True)
class Candidate:
    """One chunk a search found, with what each channel said about it."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    generation_id: uuid.UUID
    ordinal: int
    content: str
    title: str
    heading_path: str | None
    distance: float
    channels: set[str] = field(default_factory=set)
    score: float = 0.0
    lexical_tokens: frozenset[str] = frozenset()
    #: Ordinals of the chunks merged into this one, first to last.
    ordinals: tuple[int, ...] = ()
    updated_at: datetime | None = None
    #: A self-contained chunk - a catalogue row (RQ-21) - delivered alone:
    #: never merged with a neighbour and never expanded (RR-27).
    whole: bool = False


def rrf_fuse(lists: Sequence[Sequence[Candidate]], *, k: int = RRF_K) -> list[Candidate]:
    """Fuse ranked lists of candidates by Reciprocal Rank Fusion.

    A chunk found by several lists keeps its smallest distance and the union of
    its channels. Ties break by distance, then by chunk id, so the order is a
    function of the inputs and nothing else.
    """
    fused: dict[uuid.UUID, Candidate] = {}
    for ranked in lists:
        for rank, candidate in enumerate(ranked, start=1):
            held = fused.get(candidate.chunk_id)
            if held is None:
                held = replace(candidate, channels=set(candidate.channels), score=0.0)
                fused[candidate.chunk_id] = held
            else:
                held.channels |= candidate.channels
                held.distance = min(held.distance, candidate.distance)
                held.lexical_tokens = held.lexical_tokens | candidate.lexical_tokens
            held.score += 1.0 / (k + rank)
    return sorted(fused.values(), key=lambda c: (-c.score, c.distance, str(c.chunk_id)))


def keeps(candidate: Candidate, *, threshold: float, identifiers: frozenset[str]) -> bool:
    """RQ-15's keep rule: close enough, or an exact hit on one of the query's identifiers."""
    if candidate.distance <= threshold:
        return True
    return LEXICAL in candidate.channels and bool(identifiers & candidate.lexical_tokens)


def _digest(text: str) -> str:
    return hashlib.sha256(for_matching(text).encode("utf-8")).hexdigest()


def shingles(text: str) -> frozenset[tuple[str, ...]]:
    """The text's runs of `SHINGLE` consecutive words, as the near-repeat test compares them.

    Word runs rather than a bag of words (RQ-16's "80 % token overlap", measured
    on sequences): two rows of one price table share almost every word and
    differ in their numbers, and as sets they looked like repeats - the
    structural evaluation lost answers to it - while as runs they are as
    different as the rows are. The same text extracted twice still overlaps
    almost completely.
    """
    words = _TOKEN.findall(_THOUSANDS.sub("", for_lexical(text)))
    if len(words) < SHINGLE:
        return frozenset({tuple(words)}) if words else frozenset()
    return frozenset(tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1))


def _numbers(text: str) -> frozenset[str]:
    return frozenset(
        t for t in _TOKEN.findall(_THOUSANDS.sub("", for_lexical(text))) if _DIGIT.search(t)
    )


def _overlap(first: frozenset[object], second: frozenset[object]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


@dataclass(frozen=True, slots=True)
class Diversified:
    """What diversity kept, in order, and what it dropped or deferred.

    `capped` are the passages past a document's cap: deferred behind every
    other document's rather than dropped, so a workspace whose answer lives in
    one long document still receives as many of its passages as were asked for.
    """

    kept: list[Candidate]
    duplicates: int
    capped: list[Candidate]

    @property
    def ranked(self) -> list[Candidate]:
        return [*self.kept, *self.capped]


def diversify(
    candidates: Sequence[Candidate], *, per_document: int = MAX_PASSAGES_PER_DOCUMENT
) -> Diversified:
    """Drop exact and near repeats, then defer passages past a document's cap, in rank order.

    Adjacent chunks of one document share their overlap, so a near-repeat test
    between them would drop the continuation an answer needs: neighbours are
    left for `merge_adjacent`, which joins them instead.
    """
    kept: list[Candidate] = []
    seen: set[str] = set()
    token_sets: list[tuple[Candidate, frozenset[object], frozenset[str]]] = []
    per: dict[uuid.UUID, int] = {}
    duplicates = 0
    capped: list[Candidate] = []
    for candidate in candidates:
        digest = _digest(candidate.content)
        tokens: frozenset[object] = frozenset(shingles(candidate.content))
        numbers = _numbers(candidate.content)
        # A near repeat that carries a number the kept passage does not is a
        # different fact - another branch's price, next year's fee - and is
        # exactly what the model must see; only one that adds nothing goes.
        repeat = digest in seen or any(
            not _neighbours(candidate, other)
            and numbers <= other_numbers
            and _overlap(tokens, other_tokens) >= NEAR_DUPLICATE_OVERLAP
            for other, other_tokens, other_numbers in token_sets
        )
        if repeat:
            duplicates += 1
            continue
        seen.add(digest)
        token_sets.append((candidate, tokens, numbers))
        if per.get(candidate.document_id, 0) >= per_document:
            capped.append(candidate)
            continue
        per[candidate.document_id] = per.get(candidate.document_id, 0) + 1
        kept.append(candidate)
    return Diversified(kept=kept, duplicates=duplicates, capped=capped)


def _neighbours(first: Candidate, second: Candidate) -> bool:
    return first.generation_id == second.generation_id and abs(first.ordinal - second.ordinal) == 1


def join_adjacent(first: str, second: str) -> str:
    """`first` followed by `second`, without the overlap the chunker repeated at its start.

    The chunker carries whole units from the end of one chunk into the start of
    the next, so the overlap is the longest suffix of `first` that is a prefix
    of `second` and ends on a word boundary in both.
    """
    for size in range(min(len(first), len(second)), 0, -1):
        if not first.endswith(second[:size]):
            continue
        whole_in_first = size == len(first) or not first[-size - 1].isalnum()
        whole_in_second = size == len(second) or not second[size].isalnum()
        if whole_in_first and whole_in_second:
            rest = second[size:]
            return first + rest if rest[:1].isspace() or not rest else first + " " + rest.lstrip()
    return first + "\n" + second


def merge_adjacent(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Merge kept chunks that are neighbours in one generation into one passage.

    The merged passage takes the better-ranked one's place; the order inside it
    is the document's own. Only within one section: two chunks either side of a
    heading are two passages.
    """
    merged: list[Candidate] = []
    for candidate in candidates:
        into = next(
            (
                held
                for held in merged
                if held.generation_id == candidate.generation_id
                and held.heading_path == candidate.heading_path
                and not held.whole
                and not candidate.whole
                and (
                    candidate.ordinal == held.ordinals[-1] + 1
                    or candidate.ordinal == held.ordinals[0] - 1
                )
            ),
            None,
        )
        if into is None:
            merged.append(replace(candidate, ordinals=candidate.ordinals or (candidate.ordinal,)))
            continue
        if candidate.ordinal > into.ordinals[-1]:
            into.content = join_adjacent(into.content, candidate.content)
            into.ordinals = (*into.ordinals, candidate.ordinal)
        else:
            into.content = join_adjacent(candidate.content, into.content)
            into.ordinals = (candidate.ordinal, *into.ordinals)
        into.distance = min(into.distance, candidate.distance)
        into.channels |= candidate.channels
    return merged


def tier_for(best_distance: float | None, *, strong: float, partial: float) -> str:
    """strong / partial / none from the best distance and calibrated cut-offs (RQ-13)."""
    if best_distance is None:
        return NONE
    if best_distance <= strong:
        return STRONG
    if best_distance <= partial:
        return PARTIAL
    return NONE


def within_margin(candidates: Sequence[Candidate], *, margin: float | None) -> list[Candidate]:
    """Stop at passages far worse than the best one (FX-3.16). None: inert."""
    if margin is None or not candidates:
        return list(candidates)
    best = min(candidate.distance for candidate in candidates)
    return [candidate for candidate in candidates if candidate.distance <= best + margin]


__all__ = [
    "CHANNEL_CANDIDATES",
    "LEXICAL",
    "MAX_PASSAGES_PER_DOCUMENT",
    "NEAR_DUPLICATE_OVERLAP",
    "NONE",
    "PARTIAL",
    "RRF_K",
    "STRONG",
    "VECTOR",
    "Candidate",
    "Diversified",
    "diversify",
    "identifier_tokens",
    "join_adjacent",
    "keeps",
    "lexical_tokens",
    "merge_adjacent",
    "rrf_fuse",
    "shingles",
    "tier_for",
    "tsquery_text",
    "within_margin",
]
