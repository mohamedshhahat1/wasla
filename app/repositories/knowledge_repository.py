"""Data access for knowledge bases, documents, generations and chunks.

The similarity search lives here for the same reason every other query does: it
is the one place the tenant predicate can be guaranteed. A vector search that
forgot it would return another company's private documents to a customer, which
is the worst failure this system has available to it (ADR-008).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, text, update

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ConflictError
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
)
from app.db.models.tenant import Tenant
from app.repositories.base import BaseRepository, TenantScopedRepository

# Why a generation exists. Short constants, stored on the row.
TRIGGER_SUBMITTED: Final = "submitted"
TRIGGER_RESUBMITTED: Final = "resubmitted"
TRIGGER_REINDEX: Final = "reindex"
TRIGGER_STALE_EMBEDDING: Final = "stale_embedding"
TRIGGER_MIGRATED: Final = "migrated"
GENERATION_TRIGGERS: Final = frozenset(
    {
        TRIGGER_SUBMITTED,
        TRIGGER_RESUBMITTED,
        TRIGGER_REINDEX,
        TRIGGER_STALE_EMBEDDING,
        TRIGGER_MIGRATED,
    }
)


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

    async def lock(self, document_id: uuid.UUID) -> Document | None:
        """The document, row-locked for the rest of this transaction, or None.

        Every writer that changes a document's generations takes this lock
        first, and always before locking a generation, so the claim, the
        publish, a failure, a re-index request and a delete serialise on one
        row in one order and cannot deadlock one another. `populate_existing`
        because the row may already be in this session from an earlier read,
        and the decision about to be made has to be made on the locked version.
        """
        result = await self.session.execute(
            self._select()
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

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
        """Documents with nothing served and an attempt outstanding, oldest first."""
        return await self._all(
            self._select()
            .where(Document.status.in_((DocumentStatus.PENDING, DocumentStatus.PROCESSING)))
            .order_by(Document.created_at)
            .limit(limit)
        )

    def create(
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
    ) -> Document:
        return self.add(
            Document(
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
        )


class GenerationRepository(TenantScopedRepository[DocumentIndexGeneration]):
    """Indexing generations of one workspace's documents."""

    model = DocumentIndexGeneration

    def _tenant_filter(self) -> ColumnElement[bool]:
        return DocumentIndexGeneration.tenant_id == self.tenant_id

    async def in_flight(
        self, *, document_id: uuid.UUID, lock: bool = False
    ) -> DocumentIndexGeneration | None:
        """The one pending or processing generation of a document, if any."""
        statement = self._select().where(
            DocumentIndexGeneration.document_id == document_id,
            DocumentIndexGeneration.state.in_(
                (GenerationState.PENDING, GenerationState.PROCESSING)
            ),
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._first(statement)

    async def active(
        self, *, document_id: uuid.UUID, lock: bool = False
    ) -> DocumentIndexGeneration | None:
        """The generation a document serves, if any."""
        statement = self._select().where(
            DocumentIndexGeneration.document_id == document_id,
            DocumentIndexGeneration.state == GenerationState.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._first(statement)

    async def latest(self, *, document_id: uuid.UUID) -> DocumentIndexGeneration | None:
        """The most recently created generation of a document."""
        return await self._first(
            self._select()
            .where(DocumentIndexGeneration.document_id == document_id)
            .order_by(DocumentIndexGeneration.number.desc())
        )

    async def claimed(
        self, *, generation_id: uuid.UUID, token: uuid.UUID
    ) -> DocumentIndexGeneration | None:
        """A generation still held under `token`, locked, or None.

        None is the fencing answer: the generation was deleted with its
        document, superseded, or re-claimed after this holder's lease lapsed.
        Whatever the holder built is no longer wanted.
        """
        result = await self.session.execute(
            self._select()
            .where(
                DocumentIndexGeneration.id == generation_id,
                DocumentIndexGeneration.claim_token == token,
                DocumentIndexGeneration.state == GenerationState.PROCESSING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def renew(self, *, generation_id: uuid.UUID, token: uuid.UUID, now: datetime) -> bool:
        """Extend a live claim. False if it is no longer this holder's."""
        result = await self.session.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self.tenant_id,
                DocumentIndexGeneration.id == generation_id,
                DocumentIndexGeneration.claim_token == token,
                DocumentIndexGeneration.state == GenerationState.PROCESSING,
            )
            .values(claimed_at=now)
            .returning(DocumentIndexGeneration.id)
        )
        return result.first() is not None

    async def next_number(self, *, document_id: uuid.UUID) -> int:
        highest = await self.session.scalar(
            select(func.max(DocumentIndexGeneration.number)).where(
                DocumentIndexGeneration.tenant_id == self.tenant_id,
                DocumentIndexGeneration.document_id == document_id,
            )
        )
        return int(highest or 0) + 1

    def create(
        self, *, document_id: uuid.UUID, number: int, trigger: str
    ) -> DocumentIndexGeneration:
        if trigger not in GENERATION_TRIGGERS:
            raise ValueError(f"unknown generation trigger {trigger!r}")
        return self.add(
            DocumentIndexGeneration(
                tenant_id=self.tenant_id,
                document_id=document_id,
                number=number,
                state=GenerationState.PENDING,
                trigger=trigger,
                attempts=0,
                chunk_count=0,
            )
        )


class DocumentChunkRepository(TenantScopedRepository[DocumentChunk]):
    """Chunks of one workspace, and the similarity search over them."""

    model = DocumentChunk

    def _tenant_filter(self) -> ColumnElement[bool]:
        return DocumentChunk.tenant_id == self.tenant_id

    async def list_for_document(self, *, document_id: uuid.UUID) -> list[DocumentChunk]:
        """The chunks a document serves: its active generation's, in order."""
        return await self._all(
            self._select()
            .join(
                DocumentIndexGeneration,
                and_(
                    DocumentIndexGeneration.id == DocumentChunk.generation_id,
                    DocumentIndexGeneration.state == GenerationState.ACTIVE,
                ),
            )
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )

    async def count_for_document(self, *, document_id: uuid.UUID) -> int:
        """Every chunk a document owns, in any generation."""
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
        """Delete every chunk of one document, whatever generation owns it."""
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )

    async def clear_for_generation(self, *, generation_id: uuid.UUID) -> None:
        """Delete one generation's chunks, which is how a superseded one retires.

        A generation is replaced wholesale rather than merged: a document whose
        text or chunking changed has different boundaries, so matching old chunks
        to new ones is guesswork, and a stale chunk left behind would be
        retrievable text that no longer appears in the source.
        """
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.generation_id == generation_id,
            )
        )

    def add_chunk(
        self,
        *,
        document_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        generation_id: uuid.UUID,
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
                generation_id=generation_id,
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
        space: EmbeddingSpace,
        limit: int = 5,
        knowledge_base_id: uuid.UUID | None = None,
    ) -> list[ScoredChunk]:
        """Nearest chunks to an embedding, within this workspace.

        Four filters, and none of them is optional:

        - `tenant_id`, which is what stops one company's question reaching
          another company's documents. It is applied on the chunk, the joined
          generation *and* the joined document, and that duplication is
          deliberate: any one predicate is sufficient, so removing one is safe
          and removing all is a cross-tenant leak. The suite is written against
          the property rather than the implementation, so it fails only when
          every one is gone - which is correct, and is why this note exists for
          whoever is tempted to delete "the redundant filter".
        - a join to the chunk's generation restricted to `ACTIVE`, so a
          generation still building, one that failed, and one that was replaced
          contribute nothing. This is what keeps the last good version of a
          document served while a re-index builds beside it, and what makes the
          swap to the new version atomic (PD-RAG-1).
        - the generation's embedding space equal to the query's (RAG-06). Two
          models can share a width and still place text in unrelated spaces; a
          distance between a query from one and a passage from the other is
          noise ranked as relevance.
        - a non-null embedding, since a chunk without one has no position in
          the space and pgvector would have to be asked what to do with it.

        Ordering is by cosine distance, which suits normalised embeddings such
        as OpenAI's and is what `ix_document_chunks_embedding_hnsw` is built
        for. The caller bounds `limit`; so does this, because an unbounded
        `LIMIT` from a mistaken caller would be an unbounded read.
        """
        await self._prepare_the_planner()
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        query = (
            select(DocumentChunk, distance.label("distance"), Document.title)
            .join(
                DocumentIndexGeneration,
                DocumentIndexGeneration.id == DocumentChunk.generation_id,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentIndexGeneration.tenant_id == self.tenant_id,
                Document.tenant_id == self.tenant_id,
                DocumentIndexGeneration.state == GenerationState.ACTIVE,
                DocumentIndexGeneration.embedding_provider == space.provider,
                DocumentIndexGeneration.embedding_model == space.model,
                DocumentIndexGeneration.embedding_dimensions == space.dimensions,
                DocumentIndexGeneration.embedding_schema_version == space.schema_version,
                DocumentChunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(max(1, limit))
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
        than were asked for.** None of the filters above is in the vector
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


@dataclass(frozen=True, slots=True)
class DueGeneration:
    """One generation the recovery sweep may hand to a worker."""

    tenant_id: uuid.UUID
    document_id: uuid.UUID
    generation_id: uuid.UUID
    state: GenerationState
    attempts: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IndexingBacklog:
    """What the gauges report about indexing across every workspace."""

    unindexed: int = 0
    oldest_unindexed_seconds: float = 0.0
    processing: int = 0
    oldest_processing_seconds: float = 0.0
    retry_waiting: int = 0
    serving: int = 0
    failed: int = 0
    exhausted: int = 0
    stale_embedding: int = 0


def _served_workspace() -> ColumnElement[bool]:
    """A workspace that is active and not tombstoned (PD-RAG-2, PD-RAG-3)."""
    return and_(Tenant.status == TenantStatus.ACTIVE, Tenant.deleted_at.is_(None))


class IndexingSweep(BaseRepository[DocumentIndexGeneration]):
    """The unscoped reads over indexing work outstanding across every workspace.

    Deliberately not workspace-scoped, and deliberately its own class so that is
    visible - the same exception, for the same reason, that `InboundEventSweep`
    makes over the inbound log. A backlog is a platform-wide condition: the Redis
    outage or the provider outage that produced it did not choose a workspace.

    Nothing here is reachable from an API route. The callers are
    `IngestionRecoveryWorker`, the metrics exposition and the operator commands,
    and all of them are counting or finishing work rather than answering a
    person. None of them returns a document's content.
    """

    model = DocumentIndexGeneration

    async def claim_due(
        self,
        *,
        now: datetime,
        unenqueued_before: datetime,
        lease_expired_before: datetime,
        max_attempts: int,
        limit: int,
    ) -> list[DueGeneration]:
        """Generations owed a worker, locked so one sweeper publishes each.

        Eligible, and nothing else (RAG-01, RAG-10):

        - a `PENDING` generation never retried, older than the grace period -
          the upload committed and its job never reached Redis;
        - a `PENDING` generation whose `next_retry_at` has passed - a transient
          failure whose backoff is over;
        - a `PROCESSING` generation whose lease has lapsed - a worker died
          holding it. Eligible *whatever* its attempt count, so the claim that
          follows can end it as exhausted rather than leaving it held for ever.

        And only in a workspace that is active and not deleted. A suspended
        workspace's outstanding work waits, unspent, until it is reactivated; a
        deleted one's waits for the purge. A `FAILED` generation is never
        eligible - only a person asking for a re-index starts a new one.

        `FOR UPDATE OF ... SKIP LOCKED`: two sweepers divide the backlog rather
        than both publishing it. Duplicates would be harmless - the claim admits
        one worker per generation - but they would double queue depth during the
        recovery that is already behind.
        """
        pending = DocumentIndexGeneration.state == GenerationState.PENDING
        rows = await self.session.execute(
            select(
                DocumentIndexGeneration.tenant_id,
                DocumentIndexGeneration.document_id,
                DocumentIndexGeneration.id,
                DocumentIndexGeneration.state,
                DocumentIndexGeneration.attempts,
                DocumentIndexGeneration.created_at,
            )
            .join(Tenant, Tenant.id == DocumentIndexGeneration.tenant_id)
            .where(
                _served_workspace(),
                or_(
                    and_(
                        pending,
                        DocumentIndexGeneration.attempts < max_attempts,
                        or_(
                            and_(
                                DocumentIndexGeneration.next_retry_at.is_(None),
                                DocumentIndexGeneration.created_at < unenqueued_before,
                            ),
                            DocumentIndexGeneration.next_retry_at <= now,
                        ),
                    ),
                    and_(
                        DocumentIndexGeneration.state == GenerationState.PROCESSING,
                        DocumentIndexGeneration.claimed_at < lease_expired_before,
                    ),
                ),
            )
            .order_by(DocumentIndexGeneration.created_at)
            .limit(limit)
            .with_for_update(of=DocumentIndexGeneration, skip_locked=True)
        )
        return [
            DueGeneration(
                tenant_id=tenant_id,
                document_id=document_id,
                generation_id=generation_id,
                state=state,
                attempts=attempts,
                created_at=created_at,
            )
            for tenant_id, document_id, generation_id, state, attempts, created_at in rows.all()
        ]

    async def list_outstanding(self, *, limit: int) -> list[DocumentIndexGeneration]:
        """In-flight generations, oldest first, for an operator. A plain read."""
        return await self._all(
            self._select()
            .where(
                DocumentIndexGeneration.state.in_(
                    (GenerationState.PENDING, GenerationState.PROCESSING)
                )
            )
            .order_by(DocumentIndexGeneration.created_at)
            .limit(limit)
        )

    async def list_stale(
        self, *, space: EmbeddingSpace, limit: int
    ) -> list[DocumentIndexGeneration]:
        """Active generations made in a different embedding space (RAG-06)."""
        return await self._all(
            self._select()
            .where(
                DocumentIndexGeneration.state == GenerationState.ACTIVE,
                _not_in(space),
            )
            .order_by(DocumentIndexGeneration.published_at)
            .limit(limit)
        )

    async def backlog(
        self,
        *,
        now: datetime,
        unindexed_before: datetime,
        space: EmbeddingSpace | None = None,
    ) -> IndexingBacklog:
        """The counts the gauges and alerts read, in one round trip each.

        No labels come out of here but states: which workspace is a question
        for the operator command, never a time series per customer.
        """
        generation = DocumentIndexGeneration
        pending = generation.state == GenerationState.PENDING
        processing = generation.state == GenerationState.PROCESSING
        # Documents with nothing served, waiting longer than the grace period.
        # The table is read twice - the attempt and whether the document is
        # already serving something - so both sides are aliased and neither can
        # be mistaken for the other.
        outer = DocumentIndexGeneration.__table__.alias("outer_generation")
        inner = DocumentIndexGeneration.__table__.alias("inner_generation")
        # Only in served workspaces: a suspended workspace's queue is paused on
        # purpose (PD-RAG-2), and an alert on it would fire for as long as the
        # suspension lasts.
        unindexed = (
            select(func.count(outer.c.id), func.min(outer.c.created_at))
            .select_from(outer.join(Tenant, Tenant.id == outer.c.tenant_id))
            .where(_served_workspace())
        ).where(
            outer.c.state.in_((GenerationState.PENDING, GenerationState.PROCESSING)),
            outer.c.created_at < unindexed_before,
            ~select(inner.c.id)
            .where(
                inner.c.document_id == outer.c.document_id,
                inner.c.state == GenerationState.ACTIVE,
            )
            .exists(),
        )
        count, oldest = (await self.session.execute(unindexed)).one()

        held = (
            await self.session.execute(
                select(func.count(generation.id), func.min(generation.claimed_at)).where(processing)
            )
        ).one()
        waiting = await self.session.scalar(
            select(func.count(generation.id)).where(pending, generation.next_retry_at.is_not(None))
        )
        serving = await self.session.scalar(
            select(func.count(generation.id)).where(generation.state == GenerationState.ACTIVE)
        )
        # Failed attempts that are still a document's latest word: a later
        # generation of any kind means somebody already asked again.
        latest_failed = (
            select(outer.c.last_error_code)
            .where(
                outer.c.state == GenerationState.FAILED,
                ~select(inner.c.id)
                .where(
                    inner.c.document_id == outer.c.document_id,
                    inner.c.number > outer.c.number,
                )
                .exists(),
            )
            .subquery()
        )
        failures = (
            await self.session.execute(
                select(
                    func.count().filter(latest_failed.c.last_error_code == "retry_exhausted"),
                    func.count(),
                ).select_from(latest_failed)
            )
        ).one()
        stale = 0
        if space is not None:
            stale = int(
                await self.session.scalar(
                    select(func.count(generation.id)).where(
                        generation.state == GenerationState.ACTIVE, _not_in(space)
                    )
                )
                or 0
            )

        def age(moment: datetime | None) -> float:
            if moment is None:
                return 0.0
            return max((now - moment).total_seconds(), 0.0)

        exhausted, failed = int(failures[0] or 0), int(failures[1] or 0)
        return IndexingBacklog(
            unindexed=int(count or 0),
            oldest_unindexed_seconds=age(oldest) if count else 0.0,
            processing=int(held[0] or 0),
            oldest_processing_seconds=age(held[1]) if held[0] else 0.0,
            retry_waiting=int(waiting or 0),
            serving=int(serving or 0),
            failed=failed - exhausted,
            exhausted=exhausted,
            stale_embedding=stale,
        )


def _not_in(space: EmbeddingSpace) -> ColumnElement[bool]:
    return or_(
        DocumentIndexGeneration.embedding_provider != space.provider,
        DocumentIndexGeneration.embedding_model != space.model,
        DocumentIndexGeneration.embedding_dimensions != space.dimensions,
        DocumentIndexGeneration.embedding_schema_version != space.schema_version,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GENERATION_TRIGGERS",
    "TRIGGER_MIGRATED",
    "TRIGGER_REINDEX",
    "TRIGGER_RESUBMITTED",
    "TRIGGER_STALE_EMBEDDING",
    "TRIGGER_SUBMITTED",
    "DocumentChunkRepository",
    "DocumentRepository",
    "DueGeneration",
    "GenerationRepository",
    "IndexingBacklog",
    "IndexingSweep",
    "KnowledgeBaseRepository",
    "ScoredChunk",
    "utcnow",
]
