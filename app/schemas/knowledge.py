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

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.knowledge import Document, DocumentSource, DocumentStatus, KnowledgeBase
from app.services.knowledge_service import MAX_DOCUMENT_CHARACTERS


class KnowledgeBaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)


class DocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    # The document text itself. Bounded here as well as in the service, so an
    # oversized body is rejected by validation before it is read into a string.
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARACTERS)
    source: DocumentSource = DocumentSource.TEXT
    filename: str | None = Field(default=None, max_length=300)
    media_type: str | None = Field(default=None, max_length=150)


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


class DocumentRead(BaseModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source: DocumentSource
    status: DocumentStatus
    filename: str | None
    media_type: str | None
    byte_size: int
    chunk_count: int
    # Present only on a failed document, and cleared by a successful retry, so a
    # stale explanation cannot outlive the problem it described.
    error: str | None
    ingested_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, document: Document) -> Self:
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
