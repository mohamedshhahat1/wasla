"""Knowledge base administration, and the entry points into document indexing.

Submitting a document records it and asks for an index; indexing extracts,
chunks, embeds and publishes a generation (`app.services.document_indexing`).
They are separate because embedding a document is slow and calls a provider, and
no HTTP request - least of all a Meta webhook - should wait for that
(claude.md §61).

Two things *do* happen in the request, and both are bounded (RAG-02, RAG-11):
the submitted text is validated, and a PDF's text is extracted - in a separate,
killable process with page, character and time limits - because the extracted
text decides the content hash, and the hash is what makes a repeat submission a
repeat.

**Asking for an index is coalesced** (RAG-07). A document has at most one
outstanding generation, enforced by the database, so calling re-index a hundred
times while one is queued or running asks for one re-index, not a hundred full
embedding runs.

**Every mutation is audited** (RAG-17): a document is something the AI states to
customers as the business's own fact, so who added, re-indexed or removed one is
a question worth being able to answer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass
from typing import Final

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.text_safety import has_visible_content, storable_problem
from app.db.models.audit import AuditAction
from app.db.models.knowledge import (
    Document,
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
)
from app.db.models.user import User
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.knowledge_repository import (
    TRIGGER_REINDEX,
    TRIGGER_RESUBMITTED,
    TRIGGER_SUBMITTED,
    DocumentChunkRepository,
    DocumentRepository,
    GenerationRepository,
    KnowledgeBaseRepository,
)
from app.services import chunking, extraction
from app.services.audit_service import AuditTrail
from app.services.document_indexing import (
    ClaimRefusal,
    IndexingRun,
    run_indexing,
    session_units,
)
from app.services.knowledge_limits import MAX_EXTRACTED_CHARACTERS, MAX_SUBMITTED_CHARACTERS
from app.workers.ingestion_queue import IngestionJob, IngestionQueue

logger = get_logger(__name__)

DEFAULT_KNOWLEDGE_BASE_NAME: Final = "General"
# Kept under its old name for the schema that bounds the request body.
MAX_DOCUMENT_CHARACTERS: Final = MAX_SUBMITTED_CHARACTERS


@dataclass(frozen=True, slots=True)
class DocumentView:
    """A document with the two generations that describe it.

    `active` is what retrieval serves; `latest` is the most recent attempt,
    which may be the same generation, a re-index building beside it, or one
    that failed while the active one kept serving. `needs_reindex` is true when
    the active generation was embedded in a different space from the one this
    deployment now queries with (RAG-06).
    """

    document: Document
    latest: DocumentIndexGeneration | None = None
    active: DocumentIndexGeneration | None = None
    needs_reindex: bool = False


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What one in-session indexing run concluded, for callers and tests."""

    document: Document
    run: IndexingRun

    @property
    def chunks_written(self) -> int:
        return self.run.chunks

    @property
    def reused(self) -> bool:
        return self.run.refusal is ClaimRefusal.NOTHING_OUTSTANDING


def content_hash(text: str) -> str:
    """SHA-256 of the text, hex encoded.

    Computed over the extracted text rather than the uploaded bytes so that the
    same content submitted as a file and as a paste is recognised as the same
    document.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refuse_unstorable(value: str, *, field: str) -> None:
    problem = storable_problem(value)
    if problem is not None:
        raise ValidationError(f"The document {field} {problem}.")


async def extract(*, raw: str, source: DocumentSource) -> str:
    """Turn a submitted document into plain, indexable text.

    Text and Markdown are already text; Markdown keeps its punctuation because
    headings and lists are structure the chunker uses.

    A PDF arrives base64-encoded, because this endpoint takes JSON and a PDF is
    not text. It is parsed in a separate process under hard limits
    (`extraction.extract_pdf_bounded`), and refused - never truncated - past any
    of them. A scanned one, with no text layer, is refused rather than stored
    empty: an empty document looks perfectly ingested from the outside and
    answers every question with nothing, which is worse than being told the file
    needs OCR.
    """
    if source is DocumentSource.PDF:
        try:
            content = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValidationError("Submit a PDF as base64-encoded content.") from error

        text = await extraction.extract_pdf_bounded(content)
        if not text:
            raise ValidationError(
                "No text could be read from this PDF. It may be a scan, "
                "which needs to be converted to text first."
            )
        return chunking.normalise(text)

    _refuse_unstorable(raw, field="content")
    return chunking.normalise(raw)


class KnowledgeService:
    """Knowledge bases and documents for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        queue: IngestionQueue | None = None,
        space: EmbeddingSpace | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        # Optional so the ingestion worker, which is on the other end of this
        # queue, can use the service without holding a handle to it.
        self._queue = queue
        # The space this deployment queries in, for `needs_reindex`. Optional
        # because only a reader of documents needs it.
        self._space = space
        self._bases = KnowledgeBaseRepository(session, tenant_id=tenant_id)
        self._documents = DocumentRepository(session, tenant_id=tenant_id)
        self._generations = GenerationRepository(session, tenant_id=tenant_id)
        self._chunks = DocumentChunkRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
        actor: User | None = None,
    ) -> KnowledgeBase:
        base = await self._bases.create(name=name.strip(), description=description)
        await self._session.flush()
        self._audit.record(
            AuditAction.KNOWLEDGE_BASE_CREATED,
            actor=actor,
            target_type="knowledge_base",
            target_id=base.id,
            target_label=base.name,
        )
        logger.info(
            "knowledge.base_created",
            extra={"tenant_id": str(self._tenant_id), "knowledge_base_id": str(base.id)},
        )
        return base

    async def list_knowledge_bases(self, *, limit: int = 50) -> list[KnowledgeBase]:
        return await self._bases.list_all(limit=limit)

    async def get_knowledge_base(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase:
        return await self._bases.require_by_id(knowledge_base_id)

    async def ensure_default_knowledge_base(self, *, actor: User | None = None) -> KnowledgeBase:
        """The workspace's knowledge base, created on first use.

        A workspace that never thinks about knowledge bases should still be able
        to upload a document, so the first upload makes one rather than failing.
        """
        existing = await self._bases.get_by_name(DEFAULT_KNOWLEDGE_BASE_NAME)
        if existing is not None:
            return existing
        return await self.create_knowledge_base(
            name=DEFAULT_KNOWLEDGE_BASE_NAME,
            description="Documents this workspace's agents may answer from.",
            actor=actor,
        )

    async def list_documents(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        limit: int = 50,
    ) -> list[DocumentView]:
        # Resolved first so another workspace's id answers not-found rather than
        # an empty list, which would leak that the knowledge base exists.
        await self._bases.require_by_id(knowledge_base_id)
        documents = await self._documents.list_for_knowledge_base(
            knowledge_base_id=knowledge_base_id,
            limit=limit,
        )
        return await self._views(documents)

    async def get_document(self, document_id: uuid.UUID) -> DocumentView:
        document = await self._documents.require_by_id(document_id)
        (view,) = await self._views([document])
        return view

    async def submit(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        raw: str,
        source: DocumentSource = DocumentSource.TEXT,
        filename: str | None = None,
        media_type: str | None = None,
        actor: User | None = None,
    ) -> tuple[DocumentView, bool]:
        """Record a document and ask for it to be indexed. Returns it and whether it is new.

        Nothing is embedded here. Resubmitting identical text is a repeat, not a
        second document: a repeat of a document whose latest attempt failed is
        an explicit request to try again and starts a new generation; a repeat
        of anything else changes nothing.
        """
        if len(raw) > MAX_SUBMITTED_CHARACTERS:
            raise ValidationError(
                f"That document is too large. The limit is {MAX_SUBMITTED_CHARACTERS} characters."
            )
        for field, value in (("title", title), ("filename", filename), ("media type", media_type)):
            if value is not None:
                _refuse_unstorable(value, field=field)
        if not has_visible_content(title):
            raise ValidationError("The document title has no visible text.")

        await self._bases.require_by_id(knowledge_base_id)
        text = await extract(raw=raw, source=source)
        if len(text) > MAX_EXTRACTED_CHARACTERS:
            raise ValidationError(
                f"That document is too large. The limit is {MAX_EXTRACTED_CHARACTERS} characters."
            )
        if not text or not has_visible_content(text):
            raise ValidationError("That document has no text to index.")

        digest = content_hash(text)
        document = await self._documents.get_by_hash(
            knowledge_base_id=knowledge_base_id, content_hash=digest
        )
        created = document is None
        generation: DocumentIndexGeneration | None = None
        if document is None:
            document = self._documents.create(
                knowledge_base_id=knowledge_base_id,
                title=title.strip(),
                content_hash=digest,
                source=source,
                filename=filename,
                media_type=media_type,
                byte_size=len(raw.encode("utf-8")),
                content=text,
            )
            await self._session.flush()
            generation = self._generations.create(
                document_id=document.id, number=1, trigger=TRIGGER_SUBMITTED
            )
            await self._session.flush()
            await self._enqueue(document.id)
        else:
            # Locked before its generations, in the order every other writer
            # takes them, so a resubmission cannot deadlock with a worker's
            # claim on the same document.
            locked = await self._documents.lock(document.id)
            if locked is None:
                raise ConflictError("That document was removed while it was being submitted.")
            document = locked
            generation = await self._request_index(
                document, trigger=TRIGGER_RESUBMITTED, only_after_failure=True
            )

        self._audit.record(
            AuditAction.KNOWLEDGE_DOCUMENT_SUBMITTED,
            actor=actor,
            target_type="document",
            target_id=document.id,
            meta={
                "knowledge_base_id": str(knowledge_base_id),
                "source": str(source),
                "created": created,
                "generation": generation.number if generation is not None else None,
            },
        )
        logger.info(
            "knowledge.document_submitted",
            extra={
                "tenant_id": str(self._tenant_id),
                "document_id": str(document.id),
                # Not "created": LogRecord already owns that name and the
                # logging module raises rather than shadowing it.
                "is_new": created,
            },
        )
        (view,) = await self._views([document])
        return view, created

    async def reindex(
        self,
        document_id: uuid.UUID,
        *,
        actor: User | None = None,
        trigger: str = TRIGGER_REINDEX,
    ) -> DocumentView:
        """Ask for a document to be indexed again.

        How a failed document is retried once its cause is fixed, how a document
        is rebuilt after a chunking change, and how one embedded with an old
        model is moved to the current one. The document keeps serving whatever
        it serves now until the new generation publishes (PD-RAG-1).

        Coalesced: if an attempt is already outstanding, that attempt *is* the
        re-index, and no second one is created.
        """
        document = await self._documents.lock(document_id)
        if document is None:
            document = await self._documents.require_by_id(document_id)
        generation = await self._request_index(document, trigger=trigger, only_after_failure=False)
        self._audit.record(
            AuditAction.KNOWLEDGE_DOCUMENT_REINDEX_REQUESTED,
            actor=actor,
            target_type="document",
            target_id=document.id,
            meta={
                "knowledge_base_id": str(document.knowledge_base_id),
                "generation": generation.number if generation is not None else None,
                "trigger": trigger,
            },
        )
        (view,) = await self._views([document])
        return view

    async def _request_index(
        self,
        document: Document,
        *,
        trigger: str,
        only_after_failure: bool,
    ) -> DocumentIndexGeneration | None:
        """The generation that will index this document next, creating it if needed.

        An outstanding one is returned as it is, and re-enqueued if it is only
        waiting - a job lost to a Redis outage is recovered here as well as by
        the sweep, and a duplicate job costs one short claim transaction, never
        a provider call. A document already serving, when only a failure should
        start a new attempt, is left alone.
        """
        outstanding = await self._generations.in_flight(document_id=document.id, lock=True)
        if outstanding is not None:
            if outstanding.state is GenerationState.PENDING:
                await self._enqueue(document.id)
            return outstanding

        latest = await self._generations.latest(document_id=document.id)
        if only_after_failure and (latest is None or latest.state is not GenerationState.FAILED):
            return latest

        generation = self._generations.create(
            document_id=document.id,
            number=await self._generations.next_number(document_id=document.id),
            trigger=trigger,
        )
        if document.status is not DocumentStatus.READY:
            document.status = DocumentStatus.PENDING
            document.error = None
        await self._session.flush()
        await self._enqueue(document.id)
        return generation

    async def _enqueue(self, document_id: uuid.UUID) -> bool:
        """Ask a worker to index this document. Returns whether it was queued.

        A queue failure is logged and swallowed, not raised. The generation is
        already recorded as pending, which is the truth: it exists and is not
        yet searchable. Failing the request instead would discard a document the
        customer successfully uploaded because Redis was briefly unavailable,
        and `IngestionRecoveryWorker` finds anything stranded.
        """
        if self._queue is None:
            return False
        try:
            await self._queue.enqueue(
                IngestionJob(tenant_id=self._tenant_id, document_id=document_id)
            )
        except RedisError:
            logger.warning(
                "knowledge.enqueue_failed",
                extra={"document_id": str(document_id)},
            )
            return False
        return True

    async def ingest(
        self,
        *,
        document_id: uuid.UUID,
        embeddings: EmbeddingsClient,
    ) -> IngestionResult:
        """Run the indexing pipeline on this service's own session.

        Every step is flushed on the caller's transaction and nothing is
        committed. That holds the transaction open across the provider calls,
        which is exactly what the worker must not do (RAG-04) - the worker uses
        `committed_units` instead. This entry point exists for callers that own a
        transaction they intend to roll back: the state machine's own tests.
        """
        run = await run_indexing(
            session_units(self._session, tenant_id=self._tenant_id),
            document_id=document_id,
            embeddings=embeddings,
        )
        document = await self._documents.require_by_id(document_id)
        await self._session.refresh(document)
        return IngestionResult(document=document, run=run)

    async def delete_document(self, document_id: uuid.UUID, *, actor: User | None = None) -> None:
        """Remove a document and everything derived from it.

        A hard delete rather than a soft one. A workspace removing a document
        from its knowledge base is usually removing something that should no
        longer be said to customers, and a soft-deleted row that retrieval
        forgot to filter would keep saying it.

        Never waits for an embedding call (RAG-04): the lock taken here is only
        ever held elsewhere for a claim or a publish, both a few statements long.
        A worker still embedding this document finds its claim gone when it
        tries to publish, and writes nothing.
        """
        document = await self._documents.lock(document_id)
        if document is None:
            document = await self._documents.require_by_id(document_id)
        knowledge_base_id = document.knowledge_base_id
        await self._chunks.clear_for_document(document_id=document.id)
        await self._session.delete(document)
        await self._session.flush()
        self._audit.record(
            AuditAction.KNOWLEDGE_DOCUMENT_DELETED,
            actor=actor,
            target_type="document",
            target_id=document_id,
            meta={"knowledge_base_id": str(knowledge_base_id), "source": str(document.source)},
        )
        logger.info(
            "knowledge.document_deleted",
            extra={"tenant_id": str(self._tenant_id), "document_id": str(document_id)},
        )

    async def _views(self, documents: list[Document]) -> list[DocumentView]:
        """Each document with its latest and active generations, in two reads."""
        if not documents:
            return []
        ids = [document.id for document in documents]
        rows = await self._session.scalars(
            select(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self._tenant_id,
                DocumentIndexGeneration.document_id.in_(ids),
            )
            .order_by(DocumentIndexGeneration.document_id, DocumentIndexGeneration.number)
        )
        latest: dict[uuid.UUID, DocumentIndexGeneration] = {}
        active: dict[uuid.UUID, DocumentIndexGeneration] = {}
        for generation in rows:
            latest[generation.document_id] = generation
            if generation.state is GenerationState.ACTIVE:
                active[generation.document_id] = generation
        return [
            DocumentView(
                document=document,
                latest=latest.get(document.id),
                active=active.get(document.id),
                needs_reindex=self._is_stale(active.get(document.id)),
            )
            for document in documents
        ]

    def _is_stale(self, generation: DocumentIndexGeneration | None) -> bool:
        if generation is None or self._space is None:
            return False
        return (
            generation.embedding_provider,
            generation.embedding_model,
            generation.embedding_dimensions,
            generation.embedding_schema_version,
        ) != (
            self._space.provider,
            self._space.model,
            self._space.dimensions,
            self._space.schema_version,
        )


__all__ = [
    "DEFAULT_KNOWLEDGE_BASE_NAME",
    "MAX_DOCUMENT_CHARACTERS",
    "DocumentView",
    "IngestionResult",
    "KnowledgeService",
    "content_hash",
    "extract",
]
