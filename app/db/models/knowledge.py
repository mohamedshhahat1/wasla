"""Knowledge bases, their documents, indexing generations, and the chunks retrieved.

Four tables because they have four different lifetimes. A knowledge base is
configuration a workspace edits by hand. A document is a thing someone uploaded.
A **generation** is one attempt to index that document - who is working on it,
how many times it has been tried, why it last failed, which embedding model made
its vectors - and at most one generation per document is *active*, which is what
retrieval serves. A chunk is derived data belonging to exactly one generation,
thrown away with it.

Generations exist because a document's serving state and its indexing state are
two facts, and one `status` column was made to carry both (RAG-01, PD-RAG-1).
Re-indexing a working document used to set it `pending`, which hid it from every
agent for the length of the re-index - and for ever, if the re-index failed. Now
the old generation keeps serving while the new one builds, and is replaced in one
short transaction only once the new one is complete.

Knowledge is workspace-global (PD-RAG-5): an agent granted `search_knowledge`
searches every active generation in its workspace, whichever knowledge base holds
it. Knowledge bases organise documents for the people managing them; scoping an
agent to some of them is a future feature, not a filter that exists today.

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
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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
    """Whether a document is serving, as one word for the API and the dashboard.

    A summary of its generations, kept in step with them in the transaction that
    changes them:

    - `READY` - an active generation exists and is searchable. It stays `READY`
      while a re-index builds beside it and after a re-index fails.
    - `PENDING` / `PROCESSING` - nothing is served yet, and the first generation
      is waiting for, or held by, a worker.
    - `FAILED` - nothing is served and the latest generation ended terminally.
      A resting state, not a lost one: the source is kept, the generation keeps
      the reason, and an explicit re-index starts a new attempt.
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


class GenerationState(StrEnum):
    """Where one indexing attempt is.

    `ACTIVE` and `SUPERSEDED` are the serving half: the one generation retrieval
    reads, and the ones it used to. `PENDING`, `PROCESSING` and `FAILED` are the
    attempt half. A document holds at most one `ACTIVE` generation and at most
    one in flight, and the database says so (see the partial indexes below).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


#: An attempt that is outstanding - waiting for a worker or held by one.
IN_FLIGHT_GENERATION_STATES: Final = frozenset(
    {GenerationState.PENDING, GenerationState.PROCESSING}
)

DOCUMENT_STATUS_TYPE = _enum_type(DocumentStatus, name="document_status")
DOCUMENT_SOURCE_TYPE = _enum_type(DocumentSource, name="document_source")
GENERATION_STATE_TYPE = _enum_type(GenerationState, name="document_generation_state")


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
        # The target of the composite foreign keys from `documents` and
        # `document_chunks` (ADR-100). See `Contact` for why a redundant-looking
        # unique constraint is load-bearing.
        UniqueConstraint("tenant_id", "id", name="uq_knowledge_bases_tenant_id_id"),
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
        # A document belongs to a knowledge base in its own workspace, enforced
        # by PostgreSQL rather than by every writer remembering (ADR-100).
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_documents_tenant_knowledge_base",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id_id"),
        # The target of the chunk's three-column foreign key (RAG-13). With it,
        # a chunk cannot claim a knowledge base its own document is not in -
        # which the two-column keys allowed within one workspace.
        UniqueConstraint(
            "tenant_id",
            "id",
            "knowledge_base_id",
            name="uq_documents_tenant_id_id_knowledge_base_id",
        ),
    )

    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
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
    # Why the latest attempt failed, for the person who has to fix it: a copy of
    # the latest generation's, bounded and free of provider prose. Cleared when
    # a generation publishes, so a stale explanation cannot outlive the problem.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_retrievable(self) -> bool:
        """Whether this document has an active generation to serve."""
        return self.status is DocumentStatus.READY


class DocumentIndexGeneration(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One attempt to index a document, and - once published - what it serves.

    **The claim is a fencing token.** A worker takes a `PENDING` generation by
    writing a fresh `claim_token` in a short transaction, embeds with no
    transaction open, and publishes only if the token is still the one it wrote.
    A delete, a newer claim after a stale lease, or a supersession removes or
    replaces it, so a worker that returns late cannot publish what it built for
    a document that has moved on (RAG-04, RAG-09).

    **The retry budget is here, not in Redis.** `attempts` counts claims, so a
    worker dying mid-attempt spends one; `next_retry_at` is when a transient
    failure may be tried again. The recovery sweep reads both, which is what
    stops a permanently broken document being re-embedded every minute for ever
    (RAG-01).

    **The embedding space is recorded when a generation is claimed** (RAG-06),
    and retrieval compares a query only with active generations made in the
    space the query was embedded in.
    """

    __tablename__ = "document_index_generations"
    __table_args__ = (
        # Restated, not inherited: see TenantScopedMixin.
        Index("ix_document_index_generations_tenant_id", "tenant_id"),
        Index("ix_document_index_generations_document_id", "document_id"),
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "number",
            name="uq_document_index_generations_tenant_id_document_id_number",
        ),
        # The target of the chunk's foreign key, which is what pins a chunk to a
        # generation of *its own* document.
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "id",
            name="uq_document_index_generations_tenant_id_document_id_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_index_generations_tenant_document",
            ondelete="CASCADE",
        ),
        # At most one generation serves a document. The swap that replaces it
        # retires the old one in the same transaction, so a correct publish can
        # never violate this - and an incorrect one is refused here rather than
        # serving two versions of a document side by side.
        Index(
            "uq_document_index_generations_active",
            "document_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
        # At most one attempt outstanding per document. This is what coalesces a
        # burst of re-index requests into one logical re-index (RAG-07), however
        # the requests arrive.
        Index(
            "uq_document_index_generations_in_flight",
            "document_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'processing')"),
        ),
        # What the recovery sweep and the backlog gauges read. Partial, because
        # on a healthy deployment it holds only the attempts in flight.
        Index(
            "ix_document_index_generations_outstanding",
            "state",
            "next_retry_at",
            postgresql_where=text("state IN ('pending', 'processing')"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_not_negative"),
        CheckConstraint(
            "state <> 'processing' OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="processing_is_claimed",
        ),
        CheckConstraint(
            "state NOT IN ('active', 'superseded') OR ("
            "published_at IS NOT NULL AND embedding_provider IS NOT NULL "
            "AND embedding_model IS NOT NULL AND embedding_dimensions IS NOT NULL "
            "AND embedding_schema_version IS NOT NULL)",
            name="published_has_identity",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Counts up per document, so "generation 3" means something to an operator.
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[GenerationState] = mapped_column(
        GENERATION_STATE_TYPE,
        nullable=False,
        default=GenerationState.PENDING,
    )
    # Why this generation exists - one of `GENERATION_TRIGGERS` in the
    # repository. A short constant, never text a person typed.
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Renewed between embedding batches, so a live worker's claim never looks
    # abandoned and a dead worker's does within one lease.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentChunk(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One embedded passage of one generation of a document.

    Derived data. A generation's chunks are written in the transaction that
    publishes it and deleted when it is superseded, so nothing outside may hold
    a chunk id and expect it to survive a re-index.

    `knowledge_base_id` is duplicated from the document for the same reason
    `tenant_id` is: a search that scopes to one knowledge base must express that
    on the row it scans. The foreign key below makes the copy agree with its
    document rather than trusting every writer to.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        # Per generation, not per document: while a re-index publishes, the old
        # generation's ordinals and the new one's briefly coexist.
        UniqueConstraint(
            "tenant_id",
            "generation_id",
            "ordinal",
            name="uq_document_chunks_tenant_id_generation_id_ordinal",
        ),
        Index("ix_document_chunks_tenant_id", "tenant_id"),
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_generation_id", "generation_id"),
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
        # Every parent pinned. This is the table a retrieval reads, so a row here
        # claiming a foreign parent is the shape of a cross-tenant answer - and
        # `KnowledgeRepository.search` filters chunks by `knowledge_base_id`
        # directly, which makes that column a scoping field in its own right
        # rather than a denormalised convenience (ADR-100).
        #
        # Three columns to the document, not two (RAG-13): the pair
        # `(tenant_id, document_id)` let a chunk of a document in one knowledge
        # base say it belonged to another knowledge base of the same workspace.
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "knowledge_base_id"],
            ["documents.tenant_id", "documents.id", "documents.knowledge_base_id"],
            name="fk_document_chunks_tenant_document_knowledge_base",
            ondelete="CASCADE",
        ),
        # And to a generation of that same document, so a chunk cannot be
        # attached to another document's attempt.
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "generation_id"],
            [
                "document_index_generations.tenant_id",
                "document_index_generations.document_id",
                "document_index_generations.id",
            ],
            name="fk_document_chunks_tenant_document_generation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_document_chunks_tenant_knowledge_base",
            ondelete="CASCADE",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Position within the document, so retrieved passages can be cited in order
    # and a chunk can be re-read in context.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nullable in the schema and never NULL in practice: a generation's chunks
    # are written in the transaction that publishes it, with validated vectors.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=True,
    )
