"""Data access for knowledge bases, documents and chunks.

The similarity search lives here for the same reason every other query does: it
is the one place the tenant predicate can be guaranteed. A vector search that
forgot it would return another company's private documents to a customer, which
is the worst failure this system has available to it (ADR-008).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, delete, func, select, text

from app.core.exceptions import ConflictError
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)
from app.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A retrieved passage and how close it was.

    `distance` is cosine distance as pgvector reports it: 0 is identical and 2
    is opposite. It is exposed rather than converted to a similarity score
    because the threshold that decides "close enough" is a retrieval policy, and
    policy belongs above the repository.
    """

    chunk: DocumentChunk
    distance: float
    document_title: str


class KnowledgeBaseRepository(TenantScopedRepository[KnowledgeBase]):
    """Knowledge bases of one workspace."""

    model = KnowledgeBase

    def _tenant_filter(self) -> ColumnElement[bool]:
        return KnowledgeBase.tenant_id == self.tenant_id

    async def get_by_id(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase | None:
        return await self._first(self._select().where(KnowledgeBase.id == knowledge_base_id))

    async def require_by_id(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase:
        return await self._require(self._select().where(KnowledgeBase.id == knowledge_base_id))

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        return await self._first(self._select().where(KnowledgeBase.name == name))

    async def list_all(self, *, limit: int = 50) -> list[KnowledgeBase]:
        return await self._all(self._select().order_by(KnowledgeBase.name).limit(limit))

    async def create(self, *, name: str, description: str | None = None) -> KnowledgeBase:
        """Create a knowledge base, refusing a duplicate name in this workspace.

        The unique constraint is the real guarantee; this check exists to return
        a useful message rather than a driver error.
        """
        if await self.get_by_name(name) is not None:
            raise ConflictError("A knowledge base with that name already exists.")
        return self.add(KnowledgeBase(tenant_id=self.tenant_id, name=name, description=description))


class DocumentRepository(TenantScopedRepository[Document]):
    """Documents of one workspace."""

    model = Document

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Document.tenant_id == self.tenant_id

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self._first(self._select().where(Document.id == document_id))

    async def require_by_id(self, document_id: uuid.UUID) -> Document:
        return await self._require(self._select().where(Document.id == document_id))

    async def get_by_hash(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        content_hash: str,
    ) -> Document | None:
        return await self._first(
            self._select().where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.content_hash == content_hash,
            )
        )

    async def list_for_knowledge_base(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        limit: int = 50,
    ) -> list[Document]:
        return await self._all(
            self._select()
            .where(Document.knowledge_base_id == knowledge_base_id)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
        )

    async def list_pending(self, *, limit: int = 50) -> list[Document]:
        """Documents waiting to be ingested, oldest first."""
        return await self._all(
            self._select()
            .where(Document.status == DocumentStatus.PENDING)
            .order_by(Document.created_at)
            .limit(limit)
        )

    async def upsert(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        content_hash: str,
        source: DocumentSource = DocumentSource.TEXT,
        filename: str | None = None,
        media_type: str | None = None,
        byte_size: int = 0,
        content: str | None = None,
    ) -> tuple[Document, bool]:
        """Find or create the document. Returns the row and whether it is new.

        Keyed on the content hash, so uploading identical bytes twice is a
        repeat rather than a second document. A repeat of something that
        previously failed is reset to pending: the reason it failed may have
        been fixed, and refusing to retry would strand it.
        """
        existing = await self.get_by_hash(
            knowledge_base_id=knowledge_base_id,
            content_hash=content_hash,
        )
        if existing is not None:
            if existing.status is DocumentStatus.FAILED:
                existing.status = DocumentStatus.PENDING
                existing.error = None
            return existing, False

        document = Document(
            tenant_id=self.tenant_id,
            knowledge_base_id=knowledge_base_id,
            title=title,
            source=source,
            status=DocumentStatus.PENDING,
            content_hash=content_hash,
            filename=filename,
            media_type=media_type,
            byte_size=byte_size,
            content=content,
            chunk_count=0,
        )
        return self.add(document), True

    async def mark_processing(self, document: Document) -> Document:
        document.status = DocumentStatus.PROCESSING
        document.error = None
        return document

    async def mark_ready(self, document: Document, *, chunk_count: int) -> Document:
        document.status = DocumentStatus.READY
        document.chunk_count = chunk_count
        document.error = None
        document.ingested_at = datetime.now(UTC)
        return document

    async def mark_failed(self, document: Document, *, reason: str) -> Document:
        """Record why ingestion failed, keeping the document for a retry.

        The chunk count is zeroed because a failed run leaves no retrievable
        chunks; saying otherwise would advertise knowledge that is not there.
        """
        document.status = DocumentStatus.FAILED
        document.error = reason[:500]
        document.chunk_count = 0
        return document


class DocumentChunkRepository(TenantScopedRepository[DocumentChunk]):
    """Chunks of one workspace, and the similarity search over them."""

    model = DocumentChunk

    def _tenant_filter(self) -> ColumnElement[bool]:
        return DocumentChunk.tenant_id == self.tenant_id

    async def list_for_document(self, *, document_id: uuid.UUID) -> list[DocumentChunk]:
        return await self._all(
            self._select()
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )

    async def count_for_document(self, *, document_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )
        return int(result.scalar_one())

    async def clear_for_document(self, *, document_id: uuid.UUID) -> None:
        """Delete every chunk of one document.

        Re-ingestion replaces chunks wholesale rather than merging them. A
        document whose text changed has different boundaries, so matching old
        chunks to new ones is guesswork, and a stale chunk left behind would be
        retrievable text that no longer appears in the source.
        """
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )

    def add_chunk(
        self,
        *,
        document_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        ordinal: int,
        content: str,
        token_estimate: int,
        embedding: list[float] | None,
    ) -> DocumentChunk:
        return self.add(
            DocumentChunk(
                tenant_id=self.tenant_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                ordinal=ordinal,
                content=content,
                token_estimate=token_estimate,
                embedding=embedding,
            )
        )

    async def search(
        self,
        *,
        embedding: list[float],
        limit: int = 5,
        knowledge_base_id: uuid.UUID | None = None,
    ) -> list[ScoredChunk]:
        """Nearest chunks to an embedding, within this workspace.

        Three filters, and none of them is optional:

        - `tenant_id`, which is what stops one company's question reaching
          another company's documents. It is applied on *both* the chunk and
          the joined document, and that duplication is deliberate: either
          predicate alone is sufficient, so removing one is safe and removing
          both is a cross-tenant leak. The suite is written against the
          property rather than the implementation, so it fails only when both
          are gone - which is correct, and is why this note exists for whoever
          is tempted to delete "the redundant filter".
        - a join to the document restricted to `READY`, so a half-ingested or
          failed document contributes nothing. Without it, chunks written before
          a failure would still be retrievable.
        - a non-null embedding, since a chunk awaiting one has no position in
          the space and pgvector would have to be asked what to do with it.

        Ordering is by cosine distance, which suits normalised embeddings such
        as OpenAI's and is what `ix_document_chunks_embedding_hnsw` is built
        for.
        """
        await self._prepare_the_planner()
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        query = (
            select(DocumentChunk, distance.label("distance"), Document.title)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.tenant_id == self.tenant_id,
                Document.tenant_id == self.tenant_id,
                Document.status == DocumentStatus.READY,
                DocumentChunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        if knowledge_base_id is not None:
            query = query.where(DocumentChunk.knowledge_base_id == knowledge_base_id)

        result = await self.session.execute(query)
        return [
            ScoredChunk(chunk=chunk, distance=float(value), document_title=title)
            for chunk, value, title in result.all()
        ]

    async def _prepare_the_planner(self) -> None:
        """Two settings without which the index below is worse than useless.

        Both are `SET LOCAL`, so they last for this unit of work rather than
        for whatever the pooled connection is asked to do next, and both cost
        one round trip against a retrieval they turn from hundreds of
        milliseconds into single digits (ADR-079).

        **`hnsw.iterative_scan` stops the index answering with fewer passages
        than were asked for.** None of the three filters above is in the vector
        index - pgvector indexes one column - so on the approximate path they
        are applied *after* it. By default the scan visits `ef_search`
        candidates in global distance order, discards the ones belonging to
        other workspaces, and answers with whatever survived. For a workspace
        holding a small share of the corpus that is reliably nothing: the
        measured case returned **zero passages out of five** for a workspace
        with 200 chunks, and the agent was then told, as far as it could tell
        truthfully, that the knowledge base had no answer. `strict_order`
        makes the scan resume instead of stopping, and - unlike
        `relaxed_order` - hands the rows back in distance order, which is what
        the `ORDER BY` above promises its caller.

        **`plan_cache_mode` stops PostgreSQL settling on a plan built without
        knowing which workspace is asking.** This statement is prepared once
        per pooled connection and PostgreSQL compares a generic plan against
        the custom ones after five executions. The generic plan cannot know the
        tenant, so it estimates the filter from the average workspace, decides
        a nested loop over every document is cheap, and is kept for the life of
        the connection. Measured through this repository on a 45,000-chunk
        workspace: searches one to five took 7ms, and every search after that
        took 250ms - the *same* query, on the same connection, having simply
        been run often enough. Forcing a custom plan puts the workspace's real
        size back in front of the planner, which is the whole basis on which it
        chooses the approximate index at all.
        """
        # `set_config(..., is_local => true)` rather than two `SET LOCAL`
        # statements: it means the same thing and fits in one round trip, and a
        # prepared statement may carry only one command.
        await self.session.execute(
            text(
                "SELECT set_config('hnsw.iterative_scan', 'strict_order', true),"
                " set_config('plan_cache_mode', 'force_custom_plan', true)"
            )
        )
