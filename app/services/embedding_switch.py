"""Changing the embedding model without the knowledge base going dark (RQ-25, FX-4.1, RV-11).

Changing `OPENAI_EMBEDDING_MODEL` used to hide every document of every
workspace at once: a search compares a query only with generations made in the
query's space, none were, and the agent told customers the information did not
exist (RV-11). This is the supported path instead, one workspace at a time:

```
start    the workspace gets a target space (OPENAI_EMBEDDING_MODEL_NEXT); nothing
         it is served changes
build    a shadow generation per ready document, embedded in the target space by
         the ordinary worker, published beside the active one - never over it
flip     only at 100 % coverage, in one transaction: each active generation
         superseded by its shadow, and the workspace's query space moved to the
         target. Searches before the transaction see the old space whole; after,
         the new space whole
retire   the old space's chunks deleted, in batches
```

**Coverage** is every ready document holding a shadow of its *current* text (a
document replaced mid-switch needs a new one) or already active in the target.
A document whose shadow failed permanently blocks its workspace's flip and is
listed: flipping without it would hide it, which is the outage this exists to
prevent. Nothing here is time-based; every step is resumable from the database.

**Never empty, never two.** Until the flip the shadows are a state retrieval
does not read (`SHADOW`); the flip is one transaction that holds the
workspace's space row exclusively - the lock every publish takes shared before
its document - so no publish lands in the space being left; and a generation
embedded in the old space that arrives afterwards is put back to be embedded
again rather than published where no search looks (`DocumentIndexer.publish`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from redis.exceptions import RedisError
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentIndexGeneration,
    DocumentStatus,
    GenerationState,
    WorkspaceEmbeddingSpace,
)
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.repositories.knowledge_repository import (
    TRIGGER_SHADOW,
    EmbeddingSpaceRepository,
    GenerationRepository,
    query_space_of,
    target_space_of,
)
from app.services.audit_service import AuditTrail
from app.workers.ingestion_queue import IngestionJob, IngestionQueue

logger = get_logger(__name__)

# Shadow attempts that ended for a reason other than the document: they do not
# block a flip, and the builder simply makes another.
NOT_BLOCKING: Final = frozenset({"switch_cancelled", "shadow_stale", "shadow_preempted"})
# Superseded generations whose chunks one retirement statement deletes.
RETIRE_BATCH: Final = 50


class SwitchError(ConflictError):
    """An embedding-switch step that cannot run in the workspace's current state."""

    error_code = "embedding_switch_refused"


@dataclass(frozen=True, slots=True)
class BlockedDocument:
    document_id: uuid.UUID
    title: str
    code: str | None


@dataclass(frozen=True, slots=True)
class Coverage:
    """Where one workspace's switch stands."""

    tenant_id: uuid.UUID
    query_space: EmbeddingSpace
    target_space: EmbeddingSpace | None
    ready: int = 0
    covered: int = 0
    building: int = 0
    owed: int = 0
    blocked: tuple[BlockedDocument, ...] = ()
    flipped_at: datetime | None = None
    retired_at: datetime | None = None
    retiring_chunks: int = 0

    @property
    def ratio(self) -> float:
        return 1.0 if self.ready == 0 else self.covered / self.ready

    @property
    def complete(self) -> bool:
        return self.target_space is not None and self.covered == self.ready


def utcnow() -> datetime:
    return datetime.now(UTC)


class EmbeddingSwitch:
    """One workspace's embedding-model switch."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_space: EmbeddingSpace,
        queue: IngestionQueue | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default = default_space
        self._queue = queue
        self._clock = clock
        self._spaces = EmbeddingSpaceRepository(session, tenant_id=tenant_id)
        self._generations = GenerationRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def start(self, target: EmbeddingSpace) -> WorkspaceEmbeddingSpace:
        """Give the workspace a target space. What it is served does not change."""
        row = await self._spaces.row(lock="update")
        if row is None:
            row = self._spaces.create(query=self._default)
            await self._session.flush()
        query = query_space_of(row, self._default)
        current = target_space_of(row)
        if query == target:
            raise SwitchError("This workspace is already queried in that embedding space.")
        if current is not None and current != target:
            raise SwitchError(
                f"A switch to {current.describe()} is under way; cancel it first.",
            )
        row.target_provider = target.provider
        row.target_model = target.model
        row.target_dimensions = target.dimensions
        row.target_schema_version = target.schema_version
        row.switch_started_at = row.switch_started_at if current is not None else self._clock()
        row.flipped_at = None
        row.retired_at = None
        await self._session.flush()
        self._record("started", target=target)
        return row

    async def coverage(self) -> Coverage:
        """Ready documents, those with a shadow of their current text, and what blocks the rest."""
        row = await self._spaces.row()
        query = query_space_of(row, self._default)
        target = target_space_of(row)
        retiring = await self._retiring_chunks()
        if target is None:
            return Coverage(
                tenant_id=self._tenant_id,
                query_space=query,
                target_space=None,
                flipped_at=row.flipped_at if row else None,
                retired_at=row.retired_at if row else None,
                retiring_chunks=retiring,
            )
        documents = await self._ready_documents()
        covered = await self._covered(target)
        building = await self._building()
        blocked = await self._blocked(covered)
        owed = [
            d
            for d in documents
            if d.id not in covered and d.id not in building and d.id not in blocked
        ]
        return Coverage(
            tenant_id=self._tenant_id,
            query_space=query,
            target_space=target,
            ready=len(documents),
            covered=sum(1 for d in documents if d.id in covered),
            building=len(building),
            owed=len(owed),
            blocked=tuple(
                BlockedDocument(document_id=d.id, title=d.title, code=blocked[d.id])
                for d in documents
                if d.id in blocked
            ),
            flipped_at=row.flipped_at if row else None,
            retired_at=row.retired_at if row else None,
            retiring_chunks=retiring,
        )

    async def request_shadows(self, *, limit: int, retry_blocked: bool = False) -> list[uuid.UUID]:
        """Create shadow generations for up to `limit` ready documents owed one.

        A document whose shadow failed permanently is not retried unless
        `retry_blocked` - an operator who fixed the cause says so; otherwise
        every build would pay for the same failure again.
        """
        row = await self._spaces.row()
        target = target_space_of(row)
        if target is None:
            return []
        covered = await self._covered(target)
        building = await self._building()
        blocked = {} if retry_blocked else await self._blocked(covered)
        created: list[uuid.UUID] = []
        for document in await self._ready_documents():
            if len(created) >= limit:
                break
            if document.id in covered or document.id in building or document.id in blocked:
                continue
            locked = await self._session.scalar(
                select(Document)
                .where(Document.tenant_id == self._tenant_id, Document.id == document.id)
                .with_for_update()
            )
            if locked is None or locked.status is not DocumentStatus.READY:
                continue
            if await self._generations.in_flight(document_id=document.id, lock=True) is not None:
                continue
            self._generations.create(
                document_id=document.id,
                number=await self._generations.next_number(document_id=document.id),
                trigger=TRIGGER_SHADOW,
            )
            await self._session.flush()
            created.append(document.id)
        return created

    async def enqueue(self, document_ids: list[uuid.UUID]) -> int:
        """Ask a worker for each; a failed enqueue is left to the recovery sweep."""
        if self._queue is None:
            return 0
        queued = 0
        for document_id in document_ids:
            try:
                await self._queue.enqueue(
                    IngestionJob(tenant_id=self._tenant_id, document_id=document_id)
                )
                queued += 1
            except RedisError:
                logger.warning("knowledge.enqueue_failed", extra={"document_id": str(document_id)})
        return queued

    async def flip(self) -> Coverage:
        """Move the workspace's query space to the target, in one transaction, at 100 % only.

        Refused - and nothing changes - while any ready document lacks a shadow
        of its current text, including one whose shadow failed: the refusal
        lists them. Holds the workspace's space row exclusively, so no publish
        lands in either space until this commits.
        """
        row = await self._spaces.row(lock="update")
        target = target_space_of(row)
        if row is None or target is None:
            raise SwitchError("No embedding-model switch is under way in this workspace.")
        coverage = await self.coverage()
        if not coverage.complete:
            raise SwitchError(
                f"Only {coverage.covered} of {coverage.ready} ready documents have a shadow in "
                f"{target.describe()}; {len(coverage.blocked)} failed and block the flip.",
                details={
                    "covered": coverage.covered,
                    "ready": coverage.ready,
                    "blocked": [str(b.document_id) for b in coverage.blocked],
                },
            )
        now = self._clock()
        pairs = (
            await self._session.execute(
                select(DocumentIndexGeneration.document_id, DocumentIndexGeneration.id)
                .join(
                    Document,
                    and_(
                        Document.id == DocumentIndexGeneration.document_id,
                        Document.tenant_id == self._tenant_id,
                    ),
                )
                .where(
                    DocumentIndexGeneration.tenant_id == self._tenant_id,
                    DocumentIndexGeneration.state == GenerationState.SHADOW,
                    DocumentIndexGeneration.content_hash == Document.content_hash,
                    Document.status == DocumentStatus.READY,
                )
            )
        ).all()
        documents = [document_id for document_id, _ in pairs]
        shadows = [generation_id for _, generation_id in pairs]
        if documents:
            # Retired first: one active generation per document, checked per row.
            await self._session.execute(
                update(DocumentIndexGeneration)
                .where(
                    DocumentIndexGeneration.tenant_id == self._tenant_id,
                    DocumentIndexGeneration.state == GenerationState.ACTIVE,
                    DocumentIndexGeneration.document_id.in_(documents),
                )
                .values(state=GenerationState.SUPERSEDED, retired_at=now)
            )
            await self._session.execute(
                update(DocumentIndexGeneration)
                .where(
                    DocumentIndexGeneration.tenant_id == self._tenant_id,
                    DocumentIndexGeneration.id.in_(shadows),
                )
                .values(state=GenerationState.ACTIVE)
            )
            await self._session.execute(
                update(Document)
                .where(Document.tenant_id == self._tenant_id, Document.id.in_(documents))
                .values(
                    chunk_count=select(DocumentIndexGeneration.chunk_count)
                    .where(
                        DocumentIndexGeneration.document_id == Document.id,
                        DocumentIndexGeneration.state == GenerationState.ACTIVE,
                    )
                    .scalar_subquery()
                )
            )
        # Shadows of text a document no longer holds, or of a document no
        # longer ready, shadow nothing now.
        await self._session.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self._tenant_id,
                DocumentIndexGeneration.state == GenerationState.SHADOW,
            )
            .values(state=GenerationState.SUPERSEDED, retired_at=now)
        )
        row.query_provider = target.provider
        row.query_model = target.model
        row.query_dimensions = target.dimensions
        row.query_schema_version = target.schema_version
        row.target_provider = None
        row.target_model = None
        row.target_dimensions = None
        row.target_schema_version = None
        row.flipped_at = now
        row.retired_at = None
        await self._session.flush()
        self._record("flipped", target=target, documents=len(documents))
        logger.info(
            "knowledge.embedding_switch_flipped",
            extra={"tenant_id": str(self._tenant_id), "documents": len(documents)},
        )
        return await self.coverage()

    async def retire(self, *, batches: int | None = None) -> int:
        """Delete superseded generations' chunks, a batch of generations at a time.

        Returns the chunks deleted. Each batch is its own statement; the caller
        commits between calls. `retired_at` is set once none are left.
        """
        deleted = 0
        done = 0
        while batches is None or done < batches:
            generations = list(
                await self._session.scalars(
                    select(DocumentIndexGeneration.id)
                    .where(
                        DocumentIndexGeneration.tenant_id == self._tenant_id,
                        DocumentIndexGeneration.state == GenerationState.SUPERSEDED,
                        select(DocumentChunk.id)
                        .where(
                            DocumentChunk.tenant_id == self._tenant_id,
                            DocumentChunk.generation_id == DocumentIndexGeneration.id,
                        )
                        .exists(),
                    )
                    .limit(RETIRE_BATCH)
                )
            )
            if not generations:
                break
            result = await self._session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.tenant_id == self._tenant_id,
                    DocumentChunk.generation_id.in_(generations),
                )
            )
            deleted += int(result.rowcount or 0)  # type: ignore[attr-defined]
            done += 1
        if batches is None or done < batches:
            row = await self._spaces.row()
            if row is not None and row.flipped_at is not None and row.retired_at is None:
                row.retired_at = self._clock()
                self._record("retired", target=query_space_of(row, self._default))
            await self._session.flush()
        return deleted

    async def cancel(self) -> None:
        """End the switch: shadows dropped, the query space untouched."""
        row = await self._spaces.row(lock="update")
        if row is None or target_space_of(row) is None:
            raise SwitchError("No embedding-model switch is under way in this workspace.")
        target = target_space_of(row)
        now = self._clock()
        await self._session.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self._tenant_id,
                DocumentIndexGeneration.trigger == TRIGGER_SHADOW,
                DocumentIndexGeneration.state.in_(
                    (GenerationState.PENDING, GenerationState.PROCESSING)
                ),
            )
            .values(
                state=GenerationState.FAILED,
                claim_token=None,
                claimed_at=None,
                next_retry_at=None,
                last_error_code="switch_cancelled",
                last_error_at=now,
            )
        )
        await self._session.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self._tenant_id,
                DocumentIndexGeneration.state == GenerationState.SHADOW,
            )
            .values(state=GenerationState.SUPERSEDED, retired_at=now)
        )
        row.target_provider = None
        row.target_model = None
        row.target_dimensions = None
        row.target_schema_version = None
        await self._session.flush()
        if row.flipped_at is None and query_space_of(row, self._default) == self._default:
            # Never flipped: the workspace is as it was before `start`.
            await self._session.delete(row)
            await self._session.flush()
        if target is not None:
            self._record("cancelled", target=target)

    # ------------------------------------------------------------- reads

    async def _ready_documents(self) -> list[Document]:
        return list(
            await self._session.scalars(
                select(Document)
                .where(
                    Document.tenant_id == self._tenant_id, Document.status == DocumentStatus.READY
                )
                .order_by(Document.created_at, Document.id)
            )
        )

    async def _covered(self, target: EmbeddingSpace) -> set[uuid.UUID]:
        """Ready documents with a shadow of their current text, or active in the target."""
        generation = DocumentIndexGeneration
        in_target = and_(
            generation.embedding_provider == target.provider,
            generation.embedding_model == target.model,
            generation.embedding_dimensions == target.dimensions,
            generation.embedding_schema_version == target.schema_version,
        )
        rows = await self._session.scalars(
            select(generation.document_id)
            .join(
                Document,
                and_(Document.id == generation.document_id, Document.tenant_id == self._tenant_id),
            )
            .where(
                generation.tenant_id == self._tenant_id,
                Document.status == DocumentStatus.READY,
                in_target,
                or_(
                    and_(
                        generation.state == GenerationState.SHADOW,
                        generation.content_hash == Document.content_hash,
                    ),
                    generation.state == GenerationState.ACTIVE,
                ),
            )
        )
        return set(rows)

    async def _building(self) -> set[uuid.UUID]:
        rows = await self._session.scalars(
            select(DocumentIndexGeneration.document_id).where(
                DocumentIndexGeneration.tenant_id == self._tenant_id,
                DocumentIndexGeneration.state.in_(
                    (GenerationState.PENDING, GenerationState.PROCESSING)
                ),
            )
        )
        return set(rows)

    async def _blocked(self, covered: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
        """Ready documents whose latest shadow of their current text failed permanently."""
        generation = DocumentIndexGeneration
        newer = DocumentIndexGeneration.__table__.alias("newer_shadow")
        rows = await self._session.execute(
            select(generation.document_id, generation.last_error_code)
            .join(
                Document,
                and_(Document.id == generation.document_id, Document.tenant_id == self._tenant_id),
            )
            .where(
                generation.tenant_id == self._tenant_id,
                generation.trigger == TRIGGER_SHADOW,
                generation.state == GenerationState.FAILED,
                generation.content_hash == Document.content_hash,
                Document.status == DocumentStatus.READY,
                func.coalesce(generation.last_error_code, "").not_in(NOT_BLOCKING),
                ~select(newer.c.id)
                .where(
                    newer.c.document_id == generation.document_id,
                    newer.c.trigger == TRIGGER_SHADOW,
                    newer.c.number > generation.number,
                )
                .exists(),
            )
        )
        return {document_id: code for document_id, code in rows.all() if document_id not in covered}

    async def _retiring_chunks(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count(DocumentChunk.id))
                .join(
                    DocumentIndexGeneration,
                    DocumentIndexGeneration.id == DocumentChunk.generation_id,
                )
                .where(
                    DocumentChunk.tenant_id == self._tenant_id,
                    DocumentIndexGeneration.state == GenerationState.SUPERSEDED,
                )
            )
            or 0
        )

    def _record(self, step: str, *, target: EmbeddingSpace, **meta: object) -> None:
        self._audit.record(
            AuditAction.KNOWLEDGE_EMBEDDING_SWITCH,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="workspace",
            target_id=self._tenant_id,
            meta={"step": step, "space": target.describe(), **meta},
        )


@dataclass
class BuildReport:
    """What one `build` run did."""

    requested: int = 0
    queued: int = 0
    rounds: int = 0
    flipped: list[uuid.UUID] = field(default_factory=list)
    coverage: dict[uuid.UUID, Coverage] = field(default_factory=dict)


async def switching_workspaces(session: AsyncSession) -> list[uuid.UUID]:
    """Served workspaces with a switch under way. An operator read, across workspaces."""
    rows = await session.scalars(
        select(WorkspaceEmbeddingSpace.tenant_id)
        .join(Tenant, Tenant.id == WorkspaceEmbeddingSpace.tenant_id)
        .where(
            WorkspaceEmbeddingSpace.target_model.is_not(None),
            Tenant.status == TenantStatus.ACTIVE,
            Tenant.deleted_at.is_(None),
        )
        .order_by(WorkspaceEmbeddingSpace.switch_started_at, WorkspaceEmbeddingSpace.tenant_id)
    )
    return list(rows)


async def build(
    database: Database,
    *,
    default_space: EmbeddingSpace,
    queue: IngestionQueue | None,
    rate_per_minute: float,
    workspace: uuid.UUID | None = None,
    limit: int | None = None,
    flip_when_complete: bool = False,
    until_complete: bool = False,
    retry_blocked: bool = False,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    idle_seconds: float = 5.0,
) -> BuildReport:
    """The throttled background re-index of a switch: shadows requested at a bounded rate.

    One document per workspace per turn, round-robin, so a large workspace does
    not starve a small one; `rate_per_minute` across all of them. Resumable:
    what is owed is read from the database every round. With
    `until_complete`, rounds continue while any workspace still has shadows
    owed or building; with `flip_when_complete`, a workspace at 100 % is
    flipped and its old chunks retired as soon as it gets there.
    """
    report = BuildReport()
    interval = 60.0 / rate_per_minute if rate_per_minute > 0 else 0.0

    def spent() -> bool:
        return limit is not None and report.requested >= limit

    while True:
        report.rounds += 1
        async with database.session() as session:
            tenants = [workspace] if workspace else await switching_workspaces(session)
        requested = 0
        for tenant_id in tenants:
            if spent():
                break
            async with database.session() as session:
                created = await EmbeddingSwitch(
                    session, tenant_id=tenant_id, default_space=default_space
                ).request_shadows(limit=1, retry_blocked=retry_blocked)
            if not created:
                continue
            async with database.session() as session:
                report.queued += await EmbeddingSwitch(
                    session, tenant_id=tenant_id, default_space=default_space, queue=queue
                ).enqueue(created)
            report.requested += len(created)
            requested += len(created)
            if interval:
                await pause(interval)
        if spent():
            break
        if requested:
            # Another round: one more document per workspace still owed one.
            continue
        progressing = False
        for tenant_id in tenants:
            async with database.session() as session:
                coverage = await EmbeddingSwitch(
                    session, tenant_id=tenant_id, default_space=default_space
                ).coverage()
            report.coverage[tenant_id] = coverage
            if coverage.building:
                progressing = True
            if flip_when_complete and coverage.complete:
                async with database.session() as session:
                    await EmbeddingSwitch(
                        session, tenant_id=tenant_id, default_space=default_space
                    ).flip()
                report.flipped.append(tenant_id)
                await retire_all(database, tenant_id=tenant_id, default_space=default_space)
        if not until_complete or not progressing:
            break
        await pause(idle_seconds)
    for tenant_id in tenants:
        if tenant_id not in report.coverage or report.requested:
            async with database.session() as session:
                report.coverage[tenant_id] = await EmbeddingSwitch(
                    session, tenant_id=tenant_id, default_space=default_space
                ).coverage()
    return report


async def retire_all(
    database: Database, *, tenant_id: uuid.UUID, default_space: EmbeddingSpace
) -> int:
    """Retire a workspace's old chunks a batch per transaction; returns how many were deleted."""
    deleted = 0
    while True:
        async with database.session() as session:
            batch = await EmbeddingSwitch(
                session, tenant_id=tenant_id, default_space=default_space
            ).retire(batches=1)
        if not batch:
            break
        deleted += batch
    async with database.session() as session:
        await EmbeddingSwitch(session, tenant_id=tenant_id, default_space=default_space).retire(
            batches=1
        )
    return deleted


__all__ = [
    "BlockedDocument",
    "BuildReport",
    "Coverage",
    "EmbeddingSwitch",
    "SwitchError",
    "build",
    "retire_all",
    "switching_workspaces",
]
