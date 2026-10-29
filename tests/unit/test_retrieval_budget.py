"""The context budget delivers what the tool advertises (RV-15, RV-19, RQ-12, P-R06).

The passages here are cut by the real chunker from the evaluation corpus, so
"the largest passage" is the largest one this build can produce, in English, in
Arabic and in a multi-line price list.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.memory import estimate_tokens
from app.agents.registry import SEARCH_KNOWLEDGE_DEFINITION
from app.services import chunking
from app.services.chunking import CHUNK_OVERLAP_TOKENS, MAX_CHUNK_TOKENS, MIN_CHUNK_TOKENS
from app.services.retrieval_service import (
    CONTEXT_ENVELOPE_TOKENS,
    MAX_CONTEXT_TOKENS,
    MAX_TOP_K,
    MODEL_LABEL_CHARACTERS,
    PASSAGE_TOKEN_CEILING,
    Passage,
    Retrieval,
)

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "eval" / "corpus"
SAMPLES = {
    "en": ("en-returns.txt", 6),
    "ar": ("ar-policy-0.txt", 6),
    "multi-line": ("ar-prices-0.txt", 3),
}


def _largest_chunks(name: str) -> list[chunking.Chunk]:
    file, repeat = SAMPLES[name]
    text = (CORPUS / file).read_text(encoding="utf-8") * repeat
    pieces = chunking.split(text)
    return sorted(pieces, key=lambda piece: piece.token_estimate, reverse=True)


def _passages(chunks: list[chunking.Chunk], count: int, *, title: str) -> tuple[Passage, ...]:
    return tuple(
        Passage(
            document_title=title,
            content=chunks[index % len(chunks)].content,
            distance=0.1 + index / 100,
            document_id=uuid.uuid4(),
            ordinal=index,
            heading_path=chunks[index % len(chunks)].heading_path,
            updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        for index in range(count)
    )


def test_the_budget_arithmetic_holds() -> None:
    largest_chunk = MAX_CHUNK_TOKENS + CHUNK_OVERLAP_TOKENS + MIN_CHUNK_TOKENS
    # A worst-case entry: the largest chunk, newline-heavy ASCII (escaping
    # costs most there), and title and section at their clipped length in Arabic.
    worst = {
        "source": MAX_TOP_K,
        "title": "ب" * MODEL_LABEL_CHARACTERS + "…",
        "section": "ب" * MODEL_LABEL_CHARACTERS + "…",
        "updated_at": "2026-10-29",
        "match": "partial",
        "content": "a b\n" * largest_chunk,
    }
    assert estimate_tokens("a b\n" * largest_chunk) == largest_chunk
    assert estimate_tokens(json.dumps(worst, ensure_ascii=False)) <= PASSAGE_TOKEN_CEILING
    assert MAX_CONTEXT_TOKENS == MAX_TOP_K * PASSAGE_TOKEN_CEILING + CONTEXT_ENVELOPE_TOKENS
    envelope = estimate_tokens(json.dumps({"knowledge_sources": [], "note": "x" * 64}))
    assert envelope + MAX_TOP_K * 2 <= CONTEXT_ENVELOPE_TOKENS


@pytest.mark.parametrize("sample", sorted(SAMPLES))
@pytest.mark.parametrize("requested", range(1, MAX_TOP_K + 1))
def test_every_advertised_count_is_delivered(sample: str, requested: int) -> None:
    chunks = _largest_chunks(sample)
    # Non-vacuity: these are the chunker's largest pieces, close to its bound.
    assert chunks[0].token_estimate > MAX_CHUNK_TOKENS * 0.8
    retrieval = Retrieval(passages=_passages(chunks, requested, title="T" * 300), query="q")

    context = json.loads(retrieval.as_context())

    assert len(context["knowledge_sources"]) == requested
    assert estimate_tokens(retrieval.as_context()) <= MAX_CONTEXT_TOKENS


def test_the_tool_advertises_what_the_budget_delivers() -> None:
    schema = SEARCH_KNOWLEDGE_DEFINITION.json_schema()["properties"]["max_results"]
    assert schema["maximum"] == MAX_TOP_K
    assert str(MAX_TOP_K) in schema["description"]


def test_a_passage_that_does_not_fit_is_skipped_and_the_next_tried() -> None:
    """RV-19: a large rank-2 passage no longer drops a small rank-3 one."""
    large = Passage(document_title="Large", content="x " * 5_000, distance=0.1)
    larger = Passage(document_title="Larger", content="y " * 4_000, distance=0.2)
    small = Passage(document_title="Small", content="Premium finishing 7900 EGP", distance=0.3)
    # Non-vacuity: rank 1 and rank 2 cannot both fit; rank 1 and rank 3 can.
    assert estimate_tokens(large.content) + estimate_tokens(larger.content) > MAX_CONTEXT_TOKENS
    assert estimate_tokens(large.content) + estimate_tokens(small.content) < MAX_CONTEXT_TOKENS

    sources = json.loads(Retrieval(passages=(large, larger, small), query="q").as_context())

    assert [source["title"] for source in sources["knowledge_sources"]] == ["Large", "Small"]


def test_the_model_reads_source_numbers_not_document_ids() -> None:
    passage = Passage(
        document_title="Price list",
        content="Premium 7900 EGP",
        distance=0.1,
        document_id=uuid.uuid4(),
        ordinal=3,
    )
    retrieval = Retrieval(passages=(passage,), query="q")

    (source,) = json.loads(retrieval.as_context())["knowledge_sources"]

    assert source == {"source": 1, "title": "Price list", "content": "Premium 7900 EGP"}
    assert str(passage.document_id) not in retrieval.as_context()
    # The mapping stays server-side, for citations and the admin preview.
    assert retrieval.passages[0].document_id == passage.document_id
