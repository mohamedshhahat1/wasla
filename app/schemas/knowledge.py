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
from typing import Any, Literal, Self

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
    # The document's stable identity in its knowledge base (RQ-07). Absent, the
    # filename is used; uploading the same key again is a new version of it.
    source_key: str | None = Field(default=None, min_length=1, max_length=300)
    # When its facts apply (RQ-24); retrieval leaves it out otherwise.
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    # A document of the same knowledge base this one replaces as a source of truth.
    supersedes_document_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        _storable(value)
        return _visible(value)

    @field_validator("source_key")
    @classmethod
    def _source_key(cls, value: str | None) -> str | None:
        return _storable(value)

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


class DocumentReplaceRequest(BaseModel):
    """A new version of a document's content (RQ-06): replaces, never duplicates."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARACTERS)
    source: DocumentSource = DocumentSource.TEXT
    title: str | None = Field(default=None, min_length=1, max_length=300)
    filename: str | None = Field(default=None, max_length=300)
    media_type: str | None = Field(default=None, max_length=150)

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        _storable(value)
        return _visible(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _storable(value)
        return _visible(value)

    @field_validator("filename", "media_type")
    @classmethod
    def _metadata(cls, value: str | None) -> str | None:
        return _storable(value)


class KnowledgeBaseUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _storable(value)
        return _visible(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
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
    # Lines of a PDF the extractor may have re-ordered, counted per code
    # (`digits_glued_to_arabic`, `arabic_indic_leading_zero`) - never the text.
    # Such lines are indexed as extracted; re-uploading the file from another
    # tool, or re-extracting it, is how they are repaired (RQ-04).
    extraction_warnings: dict[str, int] | None = None
    source_key: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes_document_id: uuid.UUID | None = None
    # The served version's uploaded file is kept, so it can be extracted again (RQ-23).
    original_available: bool = False
    # This request changed only the title, filename or media type (RQ-09).
    metadata_updated: bool = False

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
            extraction_warnings=document.extraction_warnings,
            source_key=document.source_key,
            valid_from=document.valid_from,
            valid_until=document.valid_until,
            supersedes_document_id=document.supersedes_document_id,
            original_available=view.original_available,
            metadata_updated=view.metadata_updated,
        )


class DocumentSubmission(BaseModel):
    """A submitted document and whether it was new.

    `created` is false for a repeat submission of identical text, which is not
    an error: it tells the caller their upload was recognised rather than
    duplicated.
    """

    document: DocumentRead
    created: bool
    # For a replacement: `replacement_started` (the new version serves once it
    # is indexed) or `metadata_only` (the same text; only metadata changed).
    outcome: str | None = None


class PassageRead(BaseModel):
    """One retrieved passage, as the search preview shows it (FX-3.9).

    `source` is the number the model reads; `document_id` the mapping the server
    keeps (IMP-22). `channels` says which retrieval channel found it.
    """

    source: int
    document_id: uuid.UUID | None
    document_title: str
    section: str | None
    content: str
    distance: float
    channels: list[str]
    chunk_ordinals: list[int]
    updated_at: datetime | None


class CandidateRead(BaseModel):
    """A candidate the search saw, and what became of it."""

    document_id: uuid.UUID
    document_title: str
    chunk_ordinal: int
    distance: float
    channels: list[str]
    #: `kept`, `threshold`, `duplicate`, `margin` or `top_k` (past the count asked for,
    #: including a passage the per-document cap moved behind other documents).
    outcome: str


class SearchRequest(BaseModel):
    """What the admin preview searches for: exactly what the agent's tool takes."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    alternate_query: str | None = Field(default=None, min_length=1, max_length=2_000)
    top_k: int = Field(default=4, ge=1, le=6)
    knowledge_base_id: uuid.UUID | None = None

    @field_validator("query", "alternate_query")
    @classmethod
    def _query(cls, value: str | None) -> str | None:
        return _storable(value)


class SearchResponse(BaseModel):
    """What a search found.

    `is_empty` is stated rather than left for the caller to infer from the list,
    because it is the answer that matters: the workspace has nothing on this
    subject, and an agent must say so instead of guessing.
    """

    passages: list[PassageRead]
    is_empty: bool
    #: The confidence tier the model would be told, or None while tiers are off.
    match: str | None = None
    #: The best distance among all candidates, before any threshold.
    best_distance: float | None = None
    #: Candidates per channel, and how many were dropped for each reason.
    candidates: dict[str, int] = Field(default_factory=dict)
    dropped: dict[str, int] = Field(default_factory=dict)
    rerank: str = "disabled"
    decisions: list[CandidateRead] = Field(default_factory=list)
    #: Kept passages from two documents with the same title: often an old and
    #: a new version that should have been a replacement (RV-02).
    duplicate_version_warnings: list[str] = Field(default_factory=list)
    #: The JSON the model would read, byte for byte.
    model_context: str = ""


class KnowledgeSettingsRead(BaseModel):
    """A workspace's retrieval switches: what it set, and what is in effect (G9)."""

    overrides: dict[str, Any]
    effective: dict[str, Any]


class KnowledgeSettingsUpdate(BaseModel):
    """Overrides of the deployment's defaults; null clears one back to the default."""

    model_config = ConfigDict(extra="forbid")

    hybrid: bool | None = None
    rerank: bool | None = None
    confidence_tiers: bool | None = None
    number_grounding: Literal["off", "observe", "correct", "strict"] | None = None
    pre_retrieval: bool | None = None
    parallel_sentiment: bool | None = None
