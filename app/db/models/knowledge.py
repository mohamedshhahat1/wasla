"""Knowledge bases, their documents, and the chunks an agent retrieves.

Three tables because they have three different lifetimes. A knowledge base is
configuration a workspace edits by hand. A document is a thing someone uploaded,
with a processing state that outlives any single request. A chunk is derived
data: it can be thrown away and rebuilt from its document, and re-ingesting a
document replaces its chunks wholesale.

`tenant_id` is on all three, including the chunks, even though it could be
reached by joining through the document. Similarity search reads the chunk table
alone, and the tenant predicate has to be expressible on the row being scanned -
a filter that depends on a join is a filter someone will eventually write
without the join (ADR-008).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

# text-embedding-3-small. Fixed in the column type, so changing the embedding
# model to one with a different width is a migration, not a config edit
# (ADR-018).
EMBEDDING_DIMENSIONS: Final = 1536


class DocumentStatus(StrEnum):
    """Where a document is in the ingestion pipeline.

    `FAILED` is a resting state, not a lost one: the row keeps the reason and
    the source, so ingestion can be retried once the cause is fixed.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class DocumentSource(StrEnum):
    """How the text arrived.

    Recorded rather than inferred from a filename, because the extractor is
    chosen from this and a mislabelled extension should not silently pick the
    wrong one.
    """

    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"


DOCUMENT_STATUS_TYPE = _enum_type(DocumentStatus, name="document_status")
DOCUMENT_SOURCE_TYPE = _enum_type(DocumentSource, name="document_source")


class KnowledgeBase(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A named collection of documents belonging to one workspace.

    Exists so a workspace can keep its sales material apart from its support
    policies and point different agents at different sets. A workspace that does
    not care keeps one and never thinks about it again.
    """

    __tablename__ = "knowledge_bases"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_bases_tenant_id_name"),
        Index("ix_knowledge_bases_tenant_id", "tenant_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Document(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One uploaded source, and how far its ingestion got.

    `content_hash` is what makes ingestion idempotent. Uploading the same bytes
    to the same knowledge base twice is a repeat, not a second document, and the
    unique constraint is what enforces that rather than a check the caller could
    skip.

    The raw bytes are deliberately not stored here. Only the extracted text is
    kept, because that is all retrieval needs; an object store for originals is
    a Phase 9 concern and this table carries the metadata it would need.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "content_hash",
            name="uq_documents_tenant_id_knowledge_base_id_content_hash",
        ),
        Index("ix_documents_tenant_id", "tenant_id"),
        Index("ix_documents_tenant_id_status", "tenant_id", "status"),
        Index("ix_documents_knowledge_base_id", "knowledge_base_id"),
    )

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[DocumentSource] = mapped_column(
        DOCUMENT_SOURCE_TYPE,
        nullable=False,
        default=DocumentSource.TEXT,
    )
    status: Mapped[DocumentStatus] = mapped_column(
        DOCUMENT_STATUS_TYPE,
        nullable=False,
        default=DocumentStatus.PENDING,
    )
    # SHA-256 of the submitted bytes, hex encoded.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The extracted text, kept so a chunking change can be replayed without
    # asking the customer to upload the file again.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Why the last attempt failed, for the person who has to fix it. Cleared on
    # a successful run so a stale explanation cannot outlive the problem.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_retrievable(self) -> bool:
        """Whether this document's chunks may be returned by a search."""
        return self.status is DocumentStatus.READY


class DocumentChunk(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One embedded passage of a document.

    Derived data. Re-ingesting a document deletes every chunk it owns and writes
    new ones, so nothing outside may hold a chunk id and expect it to survive.

    `knowledge_base_id` is duplicated from the document for the same reason
    `tenant_id` is: a search that scopes to one knowledge base must express that
    on the row it scans.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "ordinal",
            name="uq_document_chunks_tenant_id_document_id_ordinal",
        ),
        Index("ix_document_chunks_tenant_id", "tenant_id"),
        Index("ix_document_chunks_document_id", "document_id"),
        Index(
            "ix_document_chunks_tenant_id_knowledge_base_id",
            "tenant_id",
            "knowledge_base_id",
        ),
        # The approximate-nearest-neighbour index, declared here as well as in
        # the migration so autogenerate compares against it and `alembic check`
        # does not offer to drop it.
        #
        # `vector_cosine_ops` because `KnowledgeRepository.search` orders by
        # `<=>`. An opclass that did not match the operator would be built,
        # catalogued, and never used - the failure mode this line exists to
        # rule out (ADR-079).
        #
        # Defaults for `m` and `ef_construction`: measured, not assumed. At
        # 45,000 chunks in one workspace the default build answers a top-5 in
        # ~2ms against ~42ms for the exact scan, and raising either parameter
        # bought nothing a retrieval can feel while costing build time on the
        # table this system writes to most.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Position within the document, so retrieved passages can be cited in order
    # and a chunk can be re-read in context.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nullable so a chunk can be written before its embedding is known, which is
    # what lets ingestion fail partway without losing the chunking work.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=True,
    )
