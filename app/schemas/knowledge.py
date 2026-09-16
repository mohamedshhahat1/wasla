"""Knowledge base API contracts.

Read models are mapped field by field rather than inferred from the ORM object,
so adding a column to a table never silently widens the API.

A document read deliberately omits `content`. The extracted text can be very
large, a list endpoint returning it would be unusable, and the API's job here is
to report what was ingested rather than to serve the text back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.text_safety import has_visible_content, storable_problem
from app.db.models.knowledge import (
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
)
from app.services.knowledge_service import MAX_DOCUMENT_CHARACTERS, DocumentView


def _storable(value: str | None) -> str | None:
    """Refuse text PostgreSQL cannot hold or Unicode cannot encode (RAG-11).

    A 422 at the boundary, naming the problem, instead of a 500 from the driver.
    """
    if value is None:
        return value
    problem = storable_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def _visible(value: str) -> str:
    if not has_visible_content(value):
        raise ValueError("must contain visible text")
    return value


class KnowledgeBaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        _storable(value)
        return _visible(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return _storable(value)


class DocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    # The document text itself. Bounded here as well as in the service, so an
    # oversized body is rejected by validation before it is read into a string.
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARACTERS)
    source: DocumentSource = DocumentSource.TEXT
    filename: str | None = Field(default=None, max_length=300)
    media_type: str | None = Field(default=None, max_length=150)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        _storable(value)
        return _visible(value)

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        # Whether a PDF has visible text is decided after extraction; this sees
        # its base64, which always does.
        _storable(value)
        return _visible(value)

    @field_validator("filename", "media_type")
    @classmethod
    def _metadata(cls, value: str | None) -> str | None:
        return _storable(value)


class KnowledgeBaseRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, base: KnowledgeBase) -> Self:
        return cls(
            id=base.id,
            name=base.name,
            description=base.description,
            created_at=base.created_at,
        )


class DocumentIndexingRead(BaseModel):
    """The latest indexing attempt, which may not be the one being served.

    Present so a person can tell "still indexing" from "retrying after a
    provider error" from "failed, and here is why" - and can see that a failed
    re-index left the previous version serving (PD-RAG-1).
    """

    generation: int
    state: GenerationState
    attempts: int
    # A code from a closed vocabulary, e.g. `provider_unauthorized`,
    # `document_too_large`, `retry_exhausted`.
    last_error_code: str | None
    next_retry_at: datetime | None
    embedding_model: str | None

    @classmethod
    def from_model(cls, generation: DocumentIndexGeneration) -> Self:
        return cls(
            generation=generation.number,
            state=generation.state,
            attempts=generation.attempts,
            last_error_code=generation.last_error_code,
            next_retry_at=generation.next_retry_at,
            embedding_model=generation.embedding_model,
        )


class DocumentRead(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source: DocumentSource
    # Whether the document serves: `ready` while any generation is active,
    # including while a re-index builds or after one fails.
    status: DocumentStatus
    filename: str | None
    media_type: str | None
    byte_size: int
    chunk_count: int
    # The latest terminal failure's explanation, bounded and free of provider
    # text. Cleared when a generation publishes, so a stale explanation cannot
    # outlive the problem it described.
    error: str | None
    ingested_at: datetime | None
    created_at: datetime
    # Which generation is serving, if any, and what the latest attempt is doing.
    serving_generation: int | None = None
    indexing: DocumentIndexingRead | None = None
    # The served generation was embedded with a model this deployment no longer
    # queries with, so it is not searched until it is re-indexed (RAG-06).
    needs_reindex: bool = False

    @classmethod
    def from_view(cls, view: DocumentView) -> Self:
        document = view.document
        return cls(
            id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            title=document.title,
            source=document.source,
            status=document.status,
            filename=document.filename,
            media_type=document.media_type,
            byte_size=document.byte_size,
            chunk_count=document.chunk_count,
            error=document.error,
            ingested_at=document.ingested_at,
            created_at=document.created_at,
            serving_generation=view.active.number if view.active is not None else None,
            indexing=(
                DocumentIndexingRead.from_model(view.latest) if view.latest is not None else None
            ),
            needs_reindex=view.needs_reindex,
        )


class DocumentSubmission(BaseModel):
    """A submitted document and whether it was new.

    `created` is false for a repeat submission of identical text, which is not
    an error: it tells the caller their upload was recognised rather than
    duplicated.
    """

    document: DocumentRead
    created: bool


class PassageRead(BaseModel):
    """One retrieved passage, for the search preview endpoint."""

    document_title: str
    content: str
    distance: float


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=4, ge=1, le=10)
    knowledge_base_id: uuid.UUID | None = None


class SearchResponse(BaseModel):
    """What a search found.

    `is_empty` is stated rather than left for the caller to infer from the list,
    because it is the answer that matters: the workspace has nothing on this
    subject, and an agent must say so instead of guessing.
    """

    passages: list[PassageRead]
    is_empty: bool
