"""Indexing limits and submission validation (RAG-02, RAG-08, RAG-11).

The worker refuses a document past the chunk or passage-character limits
before any embedding call, whatever limits it was submitted under; the
submission path refuses what cannot be stored or indexed, and refuses a PDF
over the page limit rather than truncating it.
"""

from __future__ import annotations

import base64

import pytest

from app.core.exceptions import ValidationError
from app.db.models.knowledge import DocumentSource
from app.services.document_indexing import IndexingError, IndexingFailure, split_within_limits
from app.services.knowledge_limits import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_EXTRACTED_CHARACTERS,
    MAX_PDF_PAGES,
)
from app.services.knowledge_service import extract
from tests.pdf_fixtures import paged_pdf


async def test_the_submission_path_refuses_rather_than_truncating() -> None:
    """Through `extract`, exactly as the API reaches it: no text, no READY, no call."""
    raw = base64.b64encode(paged_pdf(MAX_PDF_PAGES + 1)).decode()

    with pytest.raises(ValidationError):
        await extract(raw=raw, source=DocumentSource.PDF)


# ------------------------------------------------------------ chunks


def test_a_document_at_the_text_limit_stays_under_the_chunk_limit() -> None:
    text = "\n\n".join(f"Paragraph {n}. " + "word " * 180 for n in range(420))[
        :MAX_EXTRACTED_CHARACTERS
    ]

    pieces = split_within_limits(text)

    assert 0 < len(pieces) <= MAX_CHUNKS_PER_DOCUMENT


def test_a_document_that_splits_past_the_chunk_limit_is_refused_before_any_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.document_indexing as indexing

    monkeypatch.setattr(indexing, "MAX_CHUNKS_PER_DOCUMENT", 3)
    text = "\n\n".join(f"Paragraph {n} " + "word " * 250 for n in range(10))

    with pytest.raises(IndexingError) as raised:
        split_within_limits(text)

    assert raised.value.failure is IndexingFailure.DOCUMENT_TOO_LARGE


def test_passage_characters_are_bounded_independently_of_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.document_indexing as indexing

    monkeypatch.setattr(indexing, "MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT", 1_000)
    text = "\n\n".join("sentence " * 150 for _ in range(4))

    with pytest.raises(IndexingError) as raised:
        split_within_limits(text)

    assert raised.value.failure is IndexingFailure.DOCUMENT_TOO_LARGE


@pytest.mark.parametrize("text", ["", "   \n\n  ", "\u200b\u200b\u200b"])
def test_a_document_with_nothing_to_index_is_refused(text: str) -> None:
    with pytest.raises(IndexingError):
        split_within_limits(text)


async def test_submitted_text_with_nul_is_a_validation_error_not_a_driver_error() -> None:
    with pytest.raises(ValidationError):
        await extract(raw="policy\x00text", source=DocumentSource.TEXT)


async def test_submitted_text_with_a_lone_surrogate_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        await extract(raw="policy \udfff text", source=DocumentSource.TEXT)
