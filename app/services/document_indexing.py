"""Turning a document into an active, searchable generation - or a bounded failure.

This is the ingestion state machine, and every transition in it was rewritten
because of what the audit proved about the one it replaced.

**Three short transactions, and the provider outside all of them** (RAG-04).

```
TX1 claim     lock document -> workspace served? -> lock in-flight generation
              -> due, unheld, budget left? -> write claim token, attempts+1,
              embedding space -> commit
(no tx)       chunk -> check limits -> per batch: renew claim, embed, meter
TX2 publish   lock document -> workspace served? -> generation still ours?
              -> write chunks -> retire old active -> activate -> commit
TX3 failure   lock document -> generation still ours? -> classify ->
              retry later | failed -> commit
```

The old worker took the document's row lock, then called the embeddings API for
every batch with the transaction open, so a person deleting the document waited
for the provider and a second worker waited behind the first and then embedded
the whole document again (RAG-09). Now nothing holds a lock while a provider
thinks, and a second worker reaching the claim finds the first one's token and
leaves without calling anything.

**The claim token fences a late worker out.** A delete, a supersession or a
re-claim after an expired lease removes or replaces it. The publish and the
failure record both re-read it under the document lock, so a worker that returns
after any of those finds nothing to write to and writes nothing.

**A failure is committed on its own** (RAG-01). The old code wrote `FAILED` inside
the transaction that the failure then rolled back, so it never existed, and the
recovery sweep re-queued the still-`pending` document every minute for ever. Now
the failure is recorded in a transaction that begins after the failed work has
been abandoned, and it is classified:

- **permanent** - a rejected key, a model that does not exist, an invalid vector,
  a document past a limit, an error nobody has argued is transient: `FAILED` at
  once, with the reason, and never picked up again automatically;
- **transient** - a 5xx, a 429, a network failure, a database hiccup: back to
  `PENDING` with a jittered, growing `next_retry_at`, until the attempt budget is
  spent, and then `FAILED` as `retry_exhausted`.

**Re-indexing never takes knowledge away** (PD-RAG-1). The generation being
built is invisible to retrieval until it publishes, and the publish retires the
previous active generation in the same transaction that activates the new one.
If the new one fails, the old one is still active and the document still `READY`.

**A suspended or deleted workspace spends nothing** (PD-RAG-2, PD-RAG-3). The
claim refuses to start, each batch's renewal refuses to continue, and the publish
refuses to finish; a suspended workspace's attempt goes back to waiting, unspent,
for the day it is reactivated.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

import httpx
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import (
    DependencyUnavailableError,
    TenantIsolationError,
    ValidationError,
    WaslaError,
)
from app.core.logging import get_logger
from app.core.text_safety import has_visible_content
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    Document,
    DocumentIndexGeneration,
    DocumentStatus,
    GenerationState,
)
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.integrations.openai.embeddings import (
    MAX_BATCH,
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingRateLimitedError,
    EmbeddingsClient,
)
from app.repositories.knowledge_repository import (
    DocumentChunkRepository,
    DocumentRepository,
    GenerationRepository,
)
from app.services import chunking
from app.services.audit_service import AuditTrail
from app.services.chunking import Chunk
from app.services.knowledge_limits import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT,
)
from app.services.usage_service import EMBEDDING_PURPOSE_INGEST, UsageRecorder

logger = get_logger(__name__)

# How long a claim is honoured without being renewed. Renewed before every
# embedding batch, and one batch is bounded by the client at three attempts of
# sixty seconds with two waits of at most thirty - four minutes - so a live
# worker renews well inside this, and a dead one's claim is reclaimable within it.
CLAIM_LEASE: Final = timedelta(minutes=10)

# Attempts per generation, a claim counting as one whether it ends in a
# publish, a failure or a crash. Five with the backoff below spans roughly a
# quarter of an hour of provider trouble before the document is marked
# `retry_exhausted` - long enough for a blip or a deploy, short enough that an
# outage ends in a visible, explicit state rather than an ageing `pending`.
MAX_INDEXING_ATTEMPTS: Final = 5
RETRY_BASE: Final = timedelta(seconds=30)
RETRY_CAP: Final = timedelta(minutes=15)

MAX_ERROR_LENGTH: Final = 500


class IndexingFailure(StrEnum):
    """Failure codes that are not the embedding client's own.

    The embedding client's `EmbeddingFailure` values are stored as they are;
    these cover what the pipeline itself decides. A closed vocabulary, because a
    code is shown to operators, filtered on and counted.
    """

    DOCUMENT_EMPTY = "document_empty"
    DOCUMENT_TOO_LARGE = "document_too_large"
    INVALID_DOCUMENT = "invalid_document"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    WORKSPACE_SUSPENDED = "workspace_suspended"
    INTERNAL_ERROR = "internal_error"


class ClaimRefusal(StrEnum):
    """Why a worker did not get a generation to work on.

    Every value means *no provider call is made*. They are metric outcome
    labels too, so the set is closed.
    """

    NOTHING_OUTSTANDING = "nothing_outstanding"
    HELD = "held"
    NOT_DUE = "not_due"
    SUSPENDED = "suspended"
    EXHAUSTED = "exhausted"


class IndexingOutcome(StrEnum):
    """How one worker attempt at a document ended. A closed metric label."""

    PUBLISHED = "published"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    STALE = "stale"
    SUSPENDED = "suspended"
    SKIPPED = "skipped"


class ClaimLostError(Exception):
    """The claim stopped being this worker's while it was embedding."""


class IndexingError(WaslaError):
    """A document the pipeline refuses to index, permanently."""

    status_code = 422
    error_code = "indexing_refused"

    def __init__(self, failure: IndexingFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class Claim:
    """What a worker holds between the claim and the publish."""

    tenant_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    generation_id: uuid.UUID
    number: int
    token: uuid.UUID
    attempt: int
    content: str
    space: EmbeddingSpace


@dataclass(frozen=True, slots=True)
class Prepared:
    """A generation's chunks and their validated vectors, ready to publish."""

    pieces: tuple[Chunk, ...]
    vectors: tuple[list[float], ...]


@dataclass(frozen=True, slots=True)
class FailureRecord:
    outcome: IndexingOutcome
    code: str | None = None
    next_retry_at: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def retry_delay(attempt: int, *, jitter: float) -> timedelta:
    """How long after failed attempt `attempt` the next one may start.

    Doubling from `RETRY_BASE`, capped, with equal jitter: half the delay is
    fixed so a retry is never immediate, and half is spread so documents that
    failed together in one outage do not all come back in the same second.
    """
    computed = min(
        RETRY_BASE.total_seconds() * (2 ** max(attempt - 1, 0)), RETRY_CAP.total_seconds()
    )
    fraction = min(max(jitter, 0.0), 1.0)
    return timedelta(seconds=computed / 2 + computed / 2 * fraction)


def _failure_of(error: BaseException) -> tuple[str, bool, str]:
    """(code, permanent, safe message) for an exception raised while indexing.

    The message is always one this application wrote. Provider prose, driver
    text and exception strings from libraries never reach a row, because any of
    them can quote a key, a query or a customer's document back.
    """
    if isinstance(error, EmbeddingError):
        return str(error.failure), error.permanent, error.message
    if isinstance(error, EmbeddingRateLimitedError):
        return str(error.failure), False, error.message
    if isinstance(error, IndexingError):
        return str(error.failure), True, error.message
    if isinstance(error, ValidationError):
        return str(IndexingFailure.INVALID_DOCUMENT), True, error.message
    if isinstance(
        error,
        DependencyUnavailableError
        | RedisError
        | OperationalError
        | InterfaceError
        | ConnectionError
        | httpx.TransportError
        | TimeoutError,
    ) or (isinstance(error, DBAPIError) and error.connection_invalidated):
        return (
            str(IndexingFailure.DEPENDENCY_UNAVAILABLE),
            False,
            "A service indexing depends on was unavailable.",
        )
    # Nobody has argued that this one passes. Terminal, visible, and retryable
    # by a person once somebody has looked.
    return str(IndexingFailure.INTERNAL_ERROR), True, "Indexing failed unexpectedly."


async def _workspace_served(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[bool, bool]:
    """(active, deleted) for a workspace, read fresh."""
    row = (
        await session.execute(
            select(Tenant.status, Tenant.deleted_at).where(Tenant.id == tenant_id)
        )
    ).first()
    if row is None:
        return False, True
    status, deleted_at = row
    return status is TenantStatus.ACTIVE and deleted_at is None, deleted_at is not None


class DocumentIndexer:
    """The transactional steps of indexing one workspace's documents.

    Each method is one short unit of work on the session it is given. Nothing
    here calls a provider; `prepare` below does that, between these.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        clock: Callable[[], datetime] = utcnow,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._clock = clock
        self._jitter = jitter
        self._documents = DocumentRepository(session, tenant_id=tenant_id)
        self._generations = GenerationRepository(session, tenant_id=tenant_id)
        self._chunks = DocumentChunkRepository(session, tenant_id=tenant_id)

    async def claim(
        self,
        document_id: uuid.UUID,
        *,
        space: EmbeddingSpace,
    ) -> Claim | ClaimRefusal:
        """Take the document's outstanding generation, or say why not.

        Raises `TenantIsolationError` for a document this workspace does not
        have, which the worker's queue retries exactly once: a job can arrive a
        moment before the upload that enqueued it has committed.
        """
        document = await self._documents.lock(document_id)
        if document is None:
            raise TenantIsolationError()

        served, deleted = await _workspace_served(self._session, self._tenant_id)
        generation = await self._generations.in_flight(document_id=document.id, lock=True)
        if generation is None:
            # Already published or failed: a duplicate job, or a recovery
            # re-queue that lost a race with the worker it was backing up.
            return ClaimRefusal.NOTHING_OUTSTANDING
        if not served:
            if not deleted and generation.state is GenerationState.PENDING:
                # Visible to an operator, and spends no attempt: the workspace
                # being paused is not this document's failure.
                generation.last_error_code = str(IndexingFailure.WORKSPACE_SUSPENDED)
            return ClaimRefusal.SUSPENDED

        now = self._clock()
        if generation.state is GenerationState.PROCESSING:
            claimed_at = generation.claimed_at
            if claimed_at is not None and claimed_at > now - CLAIM_LEASE:
                return ClaimRefusal.HELD
            logger.warning(
                "knowledge.claim_lease_expired",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "document_id": str(document.id),
                    "generation": generation.number,
                    "attempts": generation.attempts,
                },
            )
        elif generation.next_retry_at is not None and generation.next_retry_at > now:
            return ClaimRefusal.NOT_DUE

        if generation.attempts >= MAX_INDEXING_ATTEMPTS:
            await self._fail(
                document,
                generation,
                code=str(IndexingFailure.RETRY_EXHAUSTED),
                message=(
                    f"Indexing did not succeed after {generation.attempts} attempts. "
                    "Re-index the document once the cause is fixed."
                ),
                now=now,
            )
            return ClaimRefusal.EXHAUSTED

        generation.state = GenerationState.PROCESSING
        generation.claim_token = uuid.uuid4()
        generation.claimed_at = now
        generation.attempts += 1
        generation.next_retry_at = None
        generation.embedding_provider = space.provider
        generation.embedding_model = space.model
        generation.embedding_dimensions = space.dimensions
        generation.embedding_schema_version = space.schema_version
        if document.status is not DocumentStatus.READY:
            document.status = DocumentStatus.PROCESSING
        await self._session.flush()

        return Claim(
            tenant_id=self._tenant_id,
            document_id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            generation_id=generation.id,
            number=generation.number,
            token=generation.claim_token,
            attempt=generation.attempts,
            content=document.content or "",
            space=space,
        )

    async def renew(self, claim: Claim) -> bool:
        """Extend the claim before another batch is paid for.

        False - stop embedding - if the generation is no longer this worker's or
        the workspace is no longer served. Checked per batch, so a document
        deleted or a workspace suspended mid-ingestion stops spending at the next
        batch boundary rather than at the end.
        """
        served, _ = await _workspace_served(self._session, self._tenant_id)
        if not served:
            return False
        return await self._generations.renew(
            generation_id=claim.generation_id, token=claim.token, now=self._clock()
        )

    def meter(self, claim: Claim, batch: EmbeddingBatch) -> None:
        """Stage the cost of one embedding request (RAG-07)."""
        UsageRecorder(self._session, tenant_id=self._tenant_id).embedding_request(
            model=claim.space.model,
            purpose=EMBEDDING_PURPOSE_INGEST,
            characters=batch.characters,
            input_tokens=batch.input_tokens,
        )

    async def publish(self, claim: Claim, prepared: Prepared) -> IndexingOutcome:
        """Make the claimed generation the one retrieval serves, atomically.

        Returns `STALE` and writes nothing when the document is gone, or the
        claim is no longer this worker's; `SUSPENDED` when the workspace stopped
        being served, putting the attempt back to wait for it.
        """
        document = await self._documents.lock(claim.document_id)
        if document is None:
            return IndexingOutcome.STALE
        generation = await self._generations.claimed(
            generation_id=claim.generation_id, token=claim.token
        )
        if generation is None:
            return IndexingOutcome.STALE

        now = self._clock()
        served, deleted = await _workspace_served(self._session, self._tenant_id)
        if not served:
            if not deleted:
                self._release(generation, code=str(IndexingFailure.WORKSPACE_SUSPENDED))
                if document.status is DocumentStatus.PROCESSING:
                    document.status = DocumentStatus.PENDING
            await self._session.flush()
            return IndexingOutcome.SUSPENDED

        for piece, vector in zip(prepared.pieces, prepared.vectors, strict=True):
            self._chunks.add_chunk(
                document_id=document.id,
                knowledge_base_id=document.knowledge_base_id,
                generation_id=generation.id,
                ordinal=piece.ordinal,
                content=piece.content,
                token_estimate=piece.token_estimate,
                embedding=vector,
            )

        previous = await self._generations.active(document_id=document.id, lock=True)
        if previous is not None:
            # Retired and flushed *before* the new one is activated. The unique
            # index allows one active generation per document, and a single
            # flush orders its updates by primary key, not by the order they
            # were made in.
            previous.state = GenerationState.SUPERSEDED
            previous.retired_at = now
            await self._session.flush()
            await self._chunks.clear_for_generation(generation_id=previous.id)

        generation.state = GenerationState.ACTIVE
        generation.chunk_count = len(prepared.pieces)
        generation.published_at = now
        generation.claim_token = None
        generation.claimed_at = None
        generation.next_retry_at = None
        generation.last_error_code = None
        generation.error = None
        document.status = DocumentStatus.READY
        document.chunk_count = len(prepared.pieces)
        document.ingested_at = now
        document.error = None
        await self._session.flush()

        logger.info(
            "knowledge.document_ingested",
            extra={
                "tenant_id": str(self._tenant_id),
                "document_id": str(document.id),
                "generation": generation.number,
                "chunks": len(prepared.pieces),
                "replaced": previous.number if previous is not None else None,
            },
        )
        return IndexingOutcome.PUBLISHED

    async def abandon(self, claim: Claim) -> IndexingOutcome:
        """Give up a claim whose renewal was refused part-way through embedding.

        Three different truths can sit behind a refused renewal, and each leaves
        something different behind: the claim was lost (`STALE`, write nothing),
        the workspace stopped being served (`SUSPENDED`, the attempt waits for
        it), or the workspace was served again by the time this runs (the
        attempt goes back to `PENDING`, due at once, and has spent its attempt).
        Never a publish: nothing was built that could be served.
        """
        document = await self._documents.lock(claim.document_id)
        if document is None:
            return IndexingOutcome.STALE
        generation = await self._generations.claimed(
            generation_id=claim.generation_id, token=claim.token
        )
        if generation is None:
            return IndexingOutcome.STALE
        served, deleted = await _workspace_served(self._session, self._tenant_id)
        if deleted:
            return IndexingOutcome.SUSPENDED
        code = IndexingFailure.WORKSPACE_SUSPENDED if not served else None
        self._release(generation, code=str(code) if code else generation.last_error_code or "")
        if code is None:
            generation.last_error_code = None
        if document.status is DocumentStatus.PROCESSING:
            document.status = DocumentStatus.PENDING
        await self._session.flush()
        return IndexingOutcome.SUSPENDED if not served else IndexingOutcome.RETRY_SCHEDULED

    async def record_failure(self, claim: Claim, error: BaseException) -> FailureRecord:
        """Write down how the claimed attempt failed, in a transaction of its own."""
        document = await self._documents.lock(claim.document_id)
        if document is None:
            return FailureRecord(outcome=IndexingOutcome.STALE)
        generation = await self._generations.claimed(
            generation_id=claim.generation_id, token=claim.token
        )
        if generation is None:
            return FailureRecord(outcome=IndexingOutcome.STALE)

        code, permanent, message = _failure_of(error)
        now = self._clock()
        logger.warning(
            "knowledge.ingestion_failed",
            extra={
                "tenant_id": str(self._tenant_id),
                "document_id": str(document.id),
                "generation": generation.number,
                "attempt": generation.attempts,
                "code": code,
                "permanent": permanent,
                "reason": type(error).__name__,
            },
        )

        if not permanent and generation.attempts < MAX_INDEXING_ATTEMPTS:
            retry_at = now + retry_delay(generation.attempts, jitter=self._jitter())
            self._release(generation, code=code, message=message, now=now)
            generation.next_retry_at = retry_at
            if document.status is DocumentStatus.PROCESSING:
                document.status = DocumentStatus.PENDING
            await self._session.flush()
            return FailureRecord(
                outcome=IndexingOutcome.RETRY_SCHEDULED, code=code, next_retry_at=retry_at
            )

        if not permanent:
            await self._fail(
                document,
                generation,
                code=str(IndexingFailure.RETRY_EXHAUSTED),
                message=f"Indexing did not succeed after {generation.attempts} attempts: {message}",
                now=now,
            )
            return FailureRecord(
                outcome=IndexingOutcome.EXHAUSTED, code=str(IndexingFailure.RETRY_EXHAUSTED)
            )

        await self._fail(document, generation, code=code, message=message, now=now)
        return FailureRecord(outcome=IndexingOutcome.FAILED, code=code)

    def _release(
        self,
        generation: DocumentIndexGeneration,
        *,
        code: str,
        message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        generation.state = GenerationState.PENDING
        generation.claim_token = None
        generation.claimed_at = None
        generation.last_error_code = code
        if message is not None:
            generation.error = message[:MAX_ERROR_LENGTH]
            generation.last_error_at = now

    async def _fail(
        self,
        document: Document,
        generation: DocumentIndexGeneration,
        *,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        """End a generation terminally. The document keeps any active one."""
        generation.state = GenerationState.FAILED
        generation.claim_token = None
        generation.claimed_at = None
        generation.next_retry_at = None
        generation.last_error_code = code
        generation.error = message[:MAX_ERROR_LENGTH]
        generation.last_error_at = now
        document.error = message[:MAX_ERROR_LENGTH]
        if document.status is not DocumentStatus.READY:
            document.status = DocumentStatus.FAILED
            document.chunk_count = 0
        AuditTrail(self._session, tenant_id=self._tenant_id).record(
            AuditAction.KNOWLEDGE_DOCUMENT_INDEXING_FAILED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="document",
            target_id=document.id,
            meta={
                "knowledge_base_id": str(document.knowledge_base_id),
                "generation": generation.number,
                "code": code,
                "attempts": generation.attempts,
                "still_serving": document.status is DocumentStatus.READY,
            },
        )
        await self._session.flush()


def split_within_limits(content: str) -> tuple[Chunk, ...]:
    """The document's chunks, refused before any cost if they exceed the limits.

    Checked here, at indexing time, and not only when a document is submitted:
    a document stored before these limits existed must not be able to spend
    what a new upload cannot (RAG-02).
    """
    if not has_visible_content(content):
        raise IndexingError(IndexingFailure.DOCUMENT_EMPTY, "That document has no text to index.")
    pieces = tuple(chunking.split(content))
    if not pieces:
        raise IndexingError(
            IndexingFailure.DOCUMENT_EMPTY, "That document produced no passages worth indexing."
        )
    if len(pieces) > MAX_CHUNKS_PER_DOCUMENT:
        raise IndexingError(
            IndexingFailure.DOCUMENT_TOO_LARGE,
            f"That document splits into {len(pieces)} passages. "
            f"The limit is {MAX_CHUNKS_PER_DOCUMENT}.",
        )
    characters = sum(len(piece.content) for piece in pieces)
    if characters > MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT:
        raise IndexingError(
            IndexingFailure.DOCUMENT_TOO_LARGE,
            f"That document is too large to index. "
            f"The limit is {MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT} characters of passages.",
        )
    return pieces


async def prepare(
    claim: Claim,
    embeddings: EmbeddingsClient,
    *,
    renew: Callable[[], Awaitable[bool]],
    meter: Callable[[EmbeddingBatch], Awaitable[None]],
) -> Prepared:
    """Chunk and embed a claimed document, with no transaction open.

    `renew` runs before each batch and `meter` after it, each in a short unit
    of work of its own. A renewal that fails raises `ClaimLostError`, and no
    further batch is paid for.
    """
    pieces = split_within_limits(claim.content)
    vectors: list[list[float]] = []
    for start in range(0, len(pieces), MAX_BATCH):
        if not await renew():
            raise ClaimLostError
        batch = await embeddings.embed_batch(
            [piece.content for piece in pieces[start : start + MAX_BATCH]]
        )
        await meter(batch)
        vectors.extend(batch.vectors)
    return Prepared(pieces=pieces, vectors=tuple(vectors))


class UnitOfWork(Protocol):
    """Opens one short transaction bound to a workspace's indexer."""

    def __call__(self) -> AbstractAsyncContextManager[DocumentIndexer]: ...


def committed_units(
    database: Database,
    *,
    tenant_id: uuid.UUID,
    clock: Callable[[], datetime] = utcnow,
) -> UnitOfWork:
    """Each unit its own session and its own commit - the worker's shape."""

    @asynccontextmanager
    async def unit() -> AsyncIterator[DocumentIndexer]:
        async with database.session() as session:
            yield DocumentIndexer(session, tenant_id=tenant_id, clock=clock)

    return unit


def session_units(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    clock: Callable[[], datetime] = utcnow,
) -> UnitOfWork:
    """Every unit on one caller-owned session, flushed and never committed.

    For callers that already own a transaction and accept holding it for the
    whole run - tests exercising the state machine on a rolled-back session.
    A worker must never use this: it is exactly the shape RAG-04 removed.
    """

    @asynccontextmanager
    async def unit() -> AsyncIterator[DocumentIndexer]:
        yield DocumentIndexer(session, tenant_id=tenant_id, clock=clock)
        await session.flush()

    return unit


@dataclass(frozen=True, slots=True)
class IndexingRun:
    """What one run of the pipeline concluded."""

    outcome: IndexingOutcome
    refusal: ClaimRefusal | None = None
    generation: int | None = None
    chunks: int = 0
    failure: FailureRecord | None = None
    error: BaseException | None = None


async def run_indexing(
    units: UnitOfWork,
    *,
    document_id: uuid.UUID,
    embeddings: EmbeddingsClient,
) -> IndexingRun:
    """Claim, embed outside any transaction, and publish or record the failure.

    Returns rather than raises for every outcome the pipeline decided, including
    failures it recorded - the document row says what happened. It raises only
    when it could not record the outcome at all (the database itself failing),
    which leaves the claim to lapse and the recovery sweep to hand it on.
    """
    async with units() as indexer:
        claimed = await indexer.claim(document_id, space=embeddings.space)
    if isinstance(claimed, ClaimRefusal):
        outcome = {
            ClaimRefusal.SUSPENDED: IndexingOutcome.SUSPENDED,
            ClaimRefusal.EXHAUSTED: IndexingOutcome.EXHAUSTED,
        }.get(claimed, IndexingOutcome.SKIPPED)
        return IndexingRun(outcome=outcome, refusal=claimed)

    claim = claimed

    async def renew() -> bool:
        async with units() as indexer:
            return await indexer.renew(claim)

    async def meter(batch: EmbeddingBatch) -> None:
        async with units() as indexer:
            indexer.meter(claim, batch)

    try:
        prepared = await prepare(claim, embeddings, renew=renew, meter=meter)
    except ClaimLostError:
        return await _after_lost_claim(units, claim)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return await _record(units, claim, error)

    try:
        async with units() as indexer:
            outcome = await indexer.publish(claim, prepared)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # The publish transaction rolled back as a whole, so the previous
        # active generation is untouched. Recorded like any other failure: a
        # database blip is retried, a defect is terminal and visible.
        return await _record(units, claim, error)
    return IndexingRun(
        outcome=outcome,
        generation=claim.number,
        chunks=len(prepared.pieces) if outcome is IndexingOutcome.PUBLISHED else 0,
    )


async def _record(units: UnitOfWork, claim: Claim, error: BaseException) -> IndexingRun:
    async with units() as indexer:
        record = await indexer.record_failure(claim, error)
    return IndexingRun(
        outcome=record.outcome,
        generation=claim.number,
        failure=record,
        error=error,
    )


async def _after_lost_claim(units: UnitOfWork, claim: Claim) -> IndexingRun:
    """A renewal refused mid-embedding: see `DocumentIndexer.abandon`."""
    async with units() as indexer:
        outcome = await indexer.abandon(claim)
    return IndexingRun(outcome=outcome, generation=claim.number)


__all__ = [
    "CLAIM_LEASE",
    "MAX_INDEXING_ATTEMPTS",
    "Claim",
    "ClaimLostError",
    "ClaimRefusal",
    "DocumentIndexer",
    "FailureRecord",
    "IndexingError",
    "IndexingFailure",
    "IndexingOutcome",
    "IndexingRun",
    "Prepared",
    "committed_units",
    "prepare",
    "retry_delay",
    "run_indexing",
    "session_units",
    "split_within_limits",
]
