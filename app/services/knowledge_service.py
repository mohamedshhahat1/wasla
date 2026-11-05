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
import dataclasses
import hashlib
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.storage import MediaStorage, StorageError, build_key, write_object
from app.core.telemetry import (
    record_extraction_warnings,
    record_format_refusal,
    record_replacement,
)
from app.core.text_safety import has_visible_content, storable_problem
from app.db.errors import is_unique_violation
from app.db.models.audit import AuditAction
from app.db.models.billing import LimitKey
from app.db.models.knowledge import (
    Document,
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
)
from app.db.models.media_purge import MediaPurgeObject
from app.db.models.user import User
from app.db.session import released
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.knowledge_repository import (
    TRIGGER_REINDEX,
    TRIGGER_REPLACED,
    TRIGGER_RESUBMITTED,
    TRIGGER_SHADOW,
    TRIGGER_SUBMITTED,
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingSpaceRepository,
    GenerationRepository,
    KnowledgeBaseRepository,
    Snapshot,
)
from app.services import document_formats, extraction, pdf_structure
from app.services.audit_service import AuditTrail
from app.services.document_indexing import (
    ClaimRefusal,
    IndexingRun,
    run_indexing,
    session_units,
)
from app.services.entitlement_service import hold_limit_lock
from app.services.knowledge_limits import MAX_EXTRACTED_CHARACTERS, MAX_SUBMITTED_CHARACTERS
from app.services.text_normalisation import for_storage
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
    # A resubmission or replacement that changed only the title, filename or
    # media type (RQ-09, RV-16).
    metadata_updated: bool = False

    @property
    def original_available(self) -> bool:
        """Whether the served version's uploaded file is kept for re-extraction (RQ-23)."""
        return self.active is not None and self.active.original_object_key is not None


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


# How a replace or a refused upload's stored original is given back: the same
# durable ledger a workspace purge uses, drained by the purge worker.
REPLACEMENT_OUTCOME_STARTED: Final = "replacement_started"
REPLACEMENT_OUTCOME_METADATA_ONLY: Final = "metadata_only"
_FILENAME_SPACE: Final = re.compile(r"\s+")


def derive_source_key(source_key: str | None, filename: str | None) -> str | None:
    """The document's stable identity in its knowledge base (RQ-07).

    An explicit key wins; otherwise the filename, NFC-normalised, case-folded
    and with whitespace collapsed, so `Pricing.PDF` and `pricing.pdf` are one
    document. No key at all leaves identity to the content hash.
    """
    chosen = source_key if source_key is not None else filename
    if chosen is None:
        return None
    key = _FILENAME_SPACE.sub(" ", unicodedata.normalize("NFC", chosen)).strip().casefold()
    return key[:300] or None


@dataclass(frozen=True, slots=True)
class Upload:
    """A submission prepared before any database work (RQ-09)."""

    text: str
    digest: str
    warnings: dict[str, int] | None
    byte_size: int
    original_key: str | None = None


@dataclass(frozen=True, slots=True)
class Extracted:
    """A submission's text as it will be stored, and what extraction flagged."""

    text: str
    warnings: dict[str, int] | None = None


def decoded_bytes(*, raw: str, source: DocumentSource) -> bytes:
    """The file a submission carries: a binary format's decoded bytes, a text's UTF-8 (RV-17)."""
    if source in document_formats.BINARY_SOURCES:
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValidationError(
                f"Submit a {source.value.upper()} file as base64-encoded content."
            ) from error
    return raw.encode("utf-8")


async def extract(*, raw: str, source: DocumentSource) -> Extracted:
    """Turn a submitted document into the text it will be stored, hashed and shown as.

    Every source ends in `for_storage` (RQ-01): NFKC, invisible controls
    removed, whitespace tidied. That is what makes an Arabic PDF whose text
    layer is presentation-form glyphs the letters a customer types (RV-01), and
    what makes a resubmission of the same words the same hash however they
    were encoded.

    Text and Markdown arrive as text. A PDF arrives base64-encoded, because
    this endpoint takes JSON and a PDF is not text. It is parsed in a separate
    process under hard limits (`extraction.extract_pdf_pages_bounded`), and
    refused - never truncated - past any of them; its pages are then joined
    with what repeats on every page removed, and lines that may have been
    re-ordered are counted (`pdf_structure`, RQ-04, RQ-05). A scanned one, with
    no text layer, is refused rather than stored empty: an empty document looks
    perfectly ingested from the outside and answers every question with
    nothing, which is worse than being told the file needs OCR.

    XLSX and DOCX arrive base64-encoded like a PDF, and with HTML are parsed in
    the same kind of bounded child (`document_formats`); CSV arrives as text
    and is parsed here. All four refuse past their limits, and refuse bytes
    that are not the format declared (RQ-21). A refusal is counted by source
    and reason, never by content.
    """
    try:
        return await _extract(raw=raw, source=source)
    except ValidationError as error:
        await record_format_refusal(str(source), document_formats.refusal_code(error))
        raise


async def _extract(*, raw: str, source: DocumentSource) -> Extracted:
    if source is DocumentSource.PDF:
        content = decoded_bytes(raw=raw, source=source)
        pages = await extraction.extract_pdf_pages_bounded(content)
        structured = pdf_structure.structure(pages)
        if not structured.text:
            raise ValidationError(
                "No text could be read from this PDF. It may be a scan, "
                "which needs to be converted to text first."
            )
        return Extracted(text=structured.text, warnings=structured.warnings or None)

    if source in (DocumentSource.XLSX, DocumentSource.DOCX, DocumentSource.HTML):
        content = decoded_bytes(raw=raw, source=source)
        text, warnings = await document_formats.extract_bounded(content, source=source)
        _refuse_unstorable(text, field="content")
        return Extracted(text=for_storage(text), warnings=warnings or None)

    _refuse_unstorable(raw, field="content")
    if source is DocumentSource.CSV:
        return Extracted(text=for_storage(document_formats.extract_csv(raw)))
    return Extracted(text=for_storage(raw))


# A failed generation that a newer replacement ended before it ran (RQ-06, D2).
REPLACED_CODE: Final = "replaced"
# A switch's shadow set aside for a re-index of the served version (RQ-25).
SHADOW_PREEMPTED_CODE: Final = "shadow_preempted"
# A knowledge base deleted with `cascade` takes at most this many documents in
# one request; more is refused part-way, and the next request continues.
MAX_DOCUMENTS_PER_DELETE: Final = 500


async def _owe_deletes(session: AsyncSession, *, tenant_id: uuid.UUID, keys: list[str]) -> None:
    """Record object deletes the purge worker owes, in the caller's transaction."""
    if not keys:
        return
    now = datetime.now(UTC)
    await session.execute(
        pg_insert(MediaPurgeObject)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "storage_key": key,
                    "not_before": now,
                    "attempts": 0,
                }
                for key in keys
            ]
        )
        .on_conflict_do_nothing(index_elements=["storage_key"])
    )


class KnowledgeService:
    """Knowledge bases and documents for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        queue: IngestionQueue | None = None,
        space: EmbeddingSpace | None = None,
        storage: MediaStorage | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        # Optional so the ingestion worker, which is on the other end of this
        # queue, can use the service without holding a handle to it.
        self._queue = queue
        # The space this deployment queries in, for `needs_reindex`. Optional
        # because only a reader of documents needs it.
        self._space = space
        # Where an upload's original file is kept (RQ-23). Optional: without a
        # store a document is indexed as before and has no original to re-read.
        self._storage = storage
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
        source_key: str | None = None,
        replace: bool = False,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        supersedes_document_id: uuid.UUID | None = None,
        reserve_slot: Callable[[], Awaitable[object]] | None = None,
        release_for_extraction: bool = False,
    ) -> tuple[DocumentView, bool]:
        """Record a document and ask for it to be indexed. Returns it and whether it is new.

        Nothing is embedded here, and the text is extracted, normalised and
        hashed before anything is locked (RQ-09): `release_for_extraction`
        hands the request's connection back while a PDF is parsed. Only then is
        the workspace's upload lock taken - the advisory lock the plan's slot
        is reserved under - and every duplicate check below runs under it, in
        the transaction that inserts, so two uploads of one text at the same
        moment end as they would one after the other (RGM-05). The plan slot
        itself - `reserve_slot` - is asked for only when a new document will
        really be created (RV-10).

        A repeat of identical text in the same knowledge base is not a second
        document: it updates a changed title, filename or media type (audited,
        `metadata_updated`), and starts a new generation only if the latest
        attempt failed. A document whose source key (given, or derived from the
        filename) already exists is refused with `document_source_exists`, or
        replaced when `replace` is set (RQ-07). Text that another knowledge base
        of this workspace already holds is refused with
        `document_exists_in_other_knowledge_base` (RQ-08): retrieval reads the
        whole workspace, so a copy would be served twice.
        """
        self._validate(raw=raw, title=title, filename=filename, media_type=media_type)
        if valid_until is not None and valid_from is not None and valid_until <= valid_from:
            raise ValidationError("valid_until must be after valid_from.")
        key = derive_source_key(source_key, filename)
        if key is not None:
            _refuse_unstorable(key, field="source key")
        upload = await self._prepare(raw=raw, source=source, release=release_for_extraction)
        try:
            view, created = await self._record(
                knowledge_base_id=knowledge_base_id,
                title=title,
                upload=upload,
                source=source,
                filename=filename,
                media_type=media_type,
                actor=actor,
                key=key,
                replace=replace,
                valid_from=valid_from,
                valid_until=valid_until,
                supersedes_document_id=supersedes_document_id,
                reserve_slot=reserve_slot,
            )
        except BaseException:
            await self._delete_unrecorded_original(upload.original_key)
            raise
        if not view.active or view.active.original_object_key != upload.original_key:
            latest = view.latest
            if latest is None or latest.original_object_key != upload.original_key:
                await self._discard_original(upload.original_key)
        return view, created

    async def _record(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        upload: Upload,
        source: DocumentSource,
        filename: str | None,
        media_type: str | None,
        actor: User | None,
        key: str | None,
        replace: bool,
        valid_from: datetime | None,
        valid_until: datetime | None,
        supersedes_document_id: uuid.UUID | None,
        reserve_slot: Callable[[], Awaitable[object]] | None,
    ) -> tuple[DocumentView, bool]:
        await self._bases.require_by_id(knowledge_base_id)
        if supersedes_document_id is not None:
            superseded = await self._documents.require_by_id(supersedes_document_id)
            if superseded.knowledge_base_id != knowledge_base_id:
                raise ValidationError(
                    "A document can only supersede one in its own knowledge base."
                )

        # The checks decide only under this lock: a check that ran before it
        # could pass for two requests at once, and both would insert (RGM-05).
        # Workspace-wide, because one of them looks across knowledge bases.
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.KNOWLEDGE_DOCUMENTS
        )
        decided = await self._existing_answer(
            knowledge_base_id=knowledge_base_id,
            title=title,
            upload=upload,
            source=source,
            filename=filename,
            media_type=media_type,
            actor=actor,
            key=key,
            replace=replace,
        )
        if decided is not None:
            return decided

        if reserve_slot is not None:
            # Only now, and only for a document that will really be created:
            # a repeat or a replacement consumes no slot (RV-10, RQ-06).
            await reserve_slot()
        try:
            async with self._session.begin_nested():
                document = self._documents.create(
                    knowledge_base_id=knowledge_base_id,
                    title=title.strip(),
                    content_hash=upload.digest,
                    source=source,
                    filename=filename,
                    media_type=media_type,
                    byte_size=upload.byte_size,
                    content=upload.text,
                )
                document.extraction_warnings = upload.warnings
                document.source_key = key
                document.valid_from = valid_from
                document.valid_until = valid_until
                document.supersedes_document_id = supersedes_document_id
                await self._session.flush()
        except IntegrityError as error:
            # Unreachable while every writer takes the lock above; should one
            # not, the request answers as the repeat it is, never as a 500.
            if not is_unique_violation(error):
                raise
            decided = await self._existing_answer(
                knowledge_base_id=knowledge_base_id,
                title=title,
                upload=upload,
                source=source,
                filename=filename,
                media_type=media_type,
                actor=actor,
                key=key,
                replace=replace,
            )
            if decided is None:
                raise
            return decided
        generation = self._generations.create(
            document_id=document.id, number=1, trigger=TRIGGER_SUBMITTED
        )
        generation.original_object_key = upload.original_key
        await self._session.flush()
        if upload.warnings:
            await record_extraction_warnings(upload.warnings)
        await self._enqueue(document.id)
        self._audit_submitted(
            document, actor=actor, source=source, created=True, generation=generation
        )
        (view,) = await self._views([document])
        return view, True

    async def _existing_answer(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        upload: Upload,
        source: DocumentSource,
        filename: str | None,
        media_type: str | None,
        actor: User | None,
        key: str | None,
        replace: bool,
    ) -> tuple[DocumentView, bool] | None:
        """The answer to an upload of text or a source this workspace already holds.

        None when the upload is new. Asked under the workspace's upload lock.
        """
        existing = await self._documents.get_by_hash(
            knowledge_base_id=knowledge_base_id, content_hash=upload.digest
        )
        if existing is not None:
            return (
                await self._resubmitted(
                    existing,
                    title=title,
                    filename=filename,
                    media_type=media_type,
                    actor=actor,
                    source=source,
                    upload=upload,
                ),
                False,
            )

        if key is not None:
            keyed = await self._documents.get_by_source_key(
                knowledge_base_id=knowledge_base_id, source_key=key
            )
            if keyed is not None:
                if not replace:
                    raise ConflictError(
                        "A document from this source already exists in this knowledge base. "
                        "Replace its content instead of uploading a second copy.",
                        error_code="document_source_exists",
                        details={
                            "document_id": str(keyed.id),
                            "replace_url": f"/api/v1/knowledge/documents/{keyed.id}/content",
                        },
                    )
                view, _ = await self._replace(
                    keyed,
                    upload=upload,
                    source=source,
                    title=title,
                    filename=filename,
                    media_type=media_type,
                    actor=actor,
                )
                return view, False

        elsewhere = await self._documents.get_by_hash_in_workspace(content_hash=upload.digest)
        if elsewhere is not None:
            raise ConflictError(
                "That exact text is already in another knowledge base of this workspace, "
                "and agents search every knowledge base, so it would be served twice.",
                error_code="document_exists_in_other_knowledge_base",
                details={
                    "document_id": str(elsewhere.id),
                    "knowledge_base_id": str(elsewhere.knowledge_base_id),
                },
            )

        return None

    async def _resubmitted(
        self,
        document: Document,
        *,
        title: str,
        filename: str | None,
        media_type: str | None,
        actor: User | None,
        source: DocumentSource,
        upload: Upload,
    ) -> DocumentView:
        # Locked before its generations, in the order every other writer
        # takes them, so a resubmission cannot deadlock with a worker's
        # claim on the same document.
        locked = await self._documents.lock(document.id)
        if locked is None:
            raise ConflictError("That document was removed while it was being submitted.")
        changed = self._update_metadata(
            locked, title=title, filename=filename, media_type=media_type, actor=actor
        )
        generation = await self._request_index(
            locked, trigger=TRIGGER_RESUBMITTED, only_after_failure=True
        )
        self._audit_submitted(
            locked, actor=actor, source=source, created=False, generation=generation
        )
        (view,) = await self._views([locked])
        return dataclasses.replace(view, metadata_updated=changed)

    async def replace_content(
        self,
        document_id: uuid.UUID,
        *,
        raw: str,
        source: DocumentSource = DocumentSource.TEXT,
        title: str | None = None,
        filename: str | None = None,
        media_type: str | None = None,
        actor: User | None = None,
        release_for_extraction: bool = False,
    ) -> tuple[DocumentView, str]:
        """Replace a document's content with a new version, never a second document (RQ-06).

        The new text is extracted and hashed before anything is locked. The
        document keeps serving its current version, and keeps its current text,
        title and hash, until a new generation carrying the new version as a
        snapshot publishes; the swap then makes the new version the only one
        served, in one transaction - never both, never neither. A pending attempt
        for the old text is ended (`replaced`); one a worker holds is fenced
        out, and cannot publish. A failed replacement leaves the old version
        serving and shows the failure on the document.

        The same text as now changes only the title, filename or media type
        (`metadata_only`). Text that another document of this workspace holds is
        refused with `document_content_exists`. No plan slot is consumed.
        """
        self._validate(
            raw=raw,
            title=title if title is not None else "x",
            filename=filename,
            media_type=media_type,
        )
        upload = await self._prepare(raw=raw, source=source, release=release_for_extraction)
        try:
            document = await self._documents.lock(document_id)
            if document is None:
                document = await self._documents.require_by_id(document_id)
            view, outcome = await self._replace(
                document,
                upload=upload,
                source=source,
                title=title,
                filename=filename,
                media_type=media_type,
                actor=actor,
            )
        except BaseException:
            await self._delete_unrecorded_original(upload.original_key)
            raise
        if outcome == REPLACEMENT_OUTCOME_METADATA_ONLY:
            await self._discard_original(upload.original_key)
        return view, outcome

    async def _replace(
        self,
        document: Document,
        *,
        upload: Upload,
        source: DocumentSource,
        title: str | None,
        filename: str | None,
        media_type: str | None,
        actor: User | None,
    ) -> tuple[DocumentView, str]:
        if upload.digest == document.content_hash:
            changed = self._update_metadata(
                document, title=title, filename=filename, media_type=media_type, actor=actor
            )
            await record_replacement(REPLACEMENT_OUTCOME_METADATA_ONLY)
            (view,) = await self._views([document])
            return (
                dataclasses.replace(view, metadata_updated=changed),
                REPLACEMENT_OUTCOME_METADATA_ONLY,
            )

        holder = await self._documents.get_by_hash_in_workspace(
            content_hash=upload.digest, exclude_document_id=document.id
        )
        if holder is not None:
            raise ConflictError(
                "Another document of this workspace already holds exactly this text.",
                error_code="document_content_exists",
                details={
                    "document_id": str(holder.id),
                    "knowledge_base_id": str(holder.knowledge_base_id),
                },
            )

        outstanding = await self._generations.in_flight(document_id=document.id, lock=True)
        if outstanding is not None:
            # Pending: it never runs. Processing: its claim token goes, so the
            # worker holding it finds the claim gone at publish and writes
            # nothing (the fencing every late worker already meets).
            outstanding.state = GenerationState.FAILED
            outstanding.claim_token = None
            outstanding.claimed_at = None
            outstanding.next_retry_at = None
            outstanding.last_error_code = REPLACED_CODE
            outstanding.error = "Replaced by a newer version before it was indexed."
            outstanding.last_error_at = datetime.now(UTC)
            await self._session.flush()
            await record_replacement("cancelled")

        snapshot = Snapshot(
            content=upload.text,
            content_hash=upload.digest,
            title=(title or document.title).strip(),
            filename=filename if filename is not None else document.filename,
            media_type=media_type if media_type is not None else document.media_type,
            byte_size=upload.byte_size,
            source=source,
            extraction_warnings=upload.warnings,
            original_object_key=upload.original_key,
        )
        generation = self._generations.create(
            document_id=document.id,
            number=await self._generations.next_number(document_id=document.id),
            trigger=TRIGGER_REPLACED,
            snapshot=snapshot,
        )
        if document.status is not DocumentStatus.READY:
            document.status = DocumentStatus.PENDING
            document.error = None
        await self._session.flush()
        if upload.warnings:
            await record_extraction_warnings(upload.warnings)
        await self._enqueue(document.id)
        self._audit.record(
            AuditAction.KNOWLEDGE_DOCUMENT_REPLACED,
            actor=actor,
            target_type="document",
            target_id=document.id,
            meta={
                "knowledge_base_id": str(document.knowledge_base_id),
                "source": str(source),
                "generation": generation.number,
                "cancelled": outstanding.number if outstanding is not None else None,
            },
        )
        logger.info(
            "knowledge.document_replacement_requested",
            extra={
                "tenant_id": str(self._tenant_id),
                "document_id": str(document.id),
                "generation": generation.number,
            },
        )
        (view,) = await self._views([document])
        return view, REPLACEMENT_OUTCOME_STARTED

    def _update_metadata(
        self,
        document: Document,
        *,
        title: str | None,
        filename: str | None,
        media_type: str | None,
        actor: User | None,
    ) -> bool:
        """Apply a changed title, filename or media type; audited. Returns whether any changed."""
        changes: dict[str, object] = {}
        if title is not None and title.strip() != document.title:
            document.title = title.strip()
            changes["title"] = True
        if filename is not None and filename != document.filename:
            document.filename = filename
            changes["filename"] = True
        if media_type is not None and media_type != document.media_type:
            document.media_type = media_type
            changes["media_type"] = True
        if changes:
            self._audit.record(
                AuditAction.KNOWLEDGE_DOCUMENT_METADATA_UPDATED,
                actor=actor,
                target_type="document",
                target_id=document.id,
                # Which fields, never their values: a title is the workspace's content.
                meta={
                    "knowledge_base_id": str(document.knowledge_base_id),
                    "fields": sorted(changes),
                },
            )
        return bool(changes)

    def _validate(
        self, *, raw: str, title: str, filename: str | None, media_type: str | None
    ) -> None:
        if len(raw) > MAX_SUBMITTED_CHARACTERS:
            raise ValidationError(
                f"That document is too large. The limit is {MAX_SUBMITTED_CHARACTERS} characters."
            )
        for field, value in (("title", title), ("filename", filename), ("media type", media_type)):
            if value is not None:
                _refuse_unstorable(value, field=field)
        if not has_visible_content(title):
            raise ValidationError("The document title has no visible text.")

    async def _prepare(self, *, raw: str, source: DocumentSource, release: bool) -> Upload:
        """Extract, normalise, hash and keep the original - holding no connection when asked."""
        if release:
            async with released(self._session):
                return await self._prepare_unlocked(raw=raw, source=source)
        return await self._prepare_unlocked(raw=raw, source=source)

    async def _prepare_unlocked(self, *, raw: str, source: DocumentSource) -> Upload:
        original = decoded_bytes(raw=raw, source=source)
        extracted = await extract(raw=raw, source=source)
        text = extracted.text
        if len(text) > MAX_EXTRACTED_CHARACTERS:
            raise ValidationError(
                f"That document is too large. The limit is {MAX_EXTRACTED_CHARACTERS} characters."
            )
        if not text or not has_visible_content(text):
            raise ValidationError("That document has no text to index.")
        key: str | None = None
        if self._storage is not None:
            mime = document_formats.MEDIA_TYPES[source]
            candidate = build_key(tenant_id=self._tenant_id, mime_type=mime)
            if await write_object(self._storage, key=candidate, data=original, mime_type=mime):
                key = candidate
        return Upload(
            text=text,
            digest=content_hash(text),
            warnings=extracted.warnings,
            byte_size=len(original),
            original_key=key,
        )

    async def _discard_original(self, key: str | None) -> None:
        """Give back an original nothing came to reference, through the purge ledger."""
        if key is None:
            return
        await _owe_deletes(self._session, tenant_id=self._tenant_id, keys=[key])

    async def _delete_unrecorded_original(self, key: str | None) -> None:
        """Delete an original whose request is failing, straight from the store.

        A refused upload - a duplicate, a full plan, a source that already
        exists - rolls its transaction back, and a purge row written in it would
        go with it, leaving the file in the store for ever (RGM-14). Nothing can
        refer to the key: it was generated for this request, and whatever was
        written with it is being rolled back too. A store that cannot be reached
        leaves the file behind, which is logged - never the refusal's answer.
        """
        if key is None or self._storage is None:
            return
        try:
            await self._storage.delete(key)
        except StorageError:
            logger.warning(
                "knowledge.original_left_behind",
                extra={"tenant_id": str(self._tenant_id), "storage_key": key},
            )

    def _audit_submitted(
        self,
        document: Document,
        *,
        actor: User | None,
        source: DocumentSource,
        created: bool,
        generation: DocumentIndexGeneration | None,
    ) -> None:
        self._audit.record(
            AuditAction.KNOWLEDGE_DOCUMENT_SUBMITTED,
            actor=actor,
            target_type="document",
            target_id=document.id,
            meta={
                "knowledge_base_id": str(document.knowledge_base_id),
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

    async def list_documents_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        knowledge_base_id: uuid.UUID | None = None,
        title: str | None = None,
        status: DocumentStatus | None = None,
        source: DocumentSource | None = None,
    ) -> tuple[list[DocumentView], str | None]:
        """Documents newest first, every one reachable by cursor (FX-2.4, RV-13)."""
        if knowledge_base_id is not None:
            await self._bases.require_by_id(knowledge_base_id)
        page = await self._documents.page(
            limit=limit,
            cursor=cursor,
            knowledge_base_id=knowledge_base_id,
            title=title,
            status=status,
            source=source,
        )
        return await self._views(page.items), page.next_cursor

    async def similar_documents(self, document_id: uuid.UUID) -> list[DocumentView]:
        """Likely other versions of a document - for "Replace existing?" (FX-2.4)."""
        document = await self._documents.require_by_id(document_id)
        return await self._views(await self._documents.similar(document))

    async def list_knowledge_bases_page(
        self, *, limit: int, cursor: str | None = None
    ) -> tuple[list[KnowledgeBase], str | None]:
        page = await self._bases.page(limit=limit, cursor=cursor)
        return page.items, page.next_cursor

    async def update_knowledge_base(
        self,
        knowledge_base_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        actor: User | None = None,
    ) -> KnowledgeBase:
        base = await self._bases.require_by_id(knowledge_base_id)
        fields = []
        if name is not None and name.strip() != base.name:
            base.name = name.strip()
            fields.append("name")
        if description is not None and description != base.description:
            base.description = description
            fields.append("description")
        if fields:
            await self._session.flush()
            self._audit.record(
                AuditAction.KNOWLEDGE_BASE_UPDATED,
                actor=actor,
                target_type="knowledge_base",
                target_id=base.id,
                target_label=base.name,
                meta={"fields": fields},
            )
        return base

    async def delete_knowledge_base(
        self, knowledge_base_id: uuid.UUID, *, cascade: bool = False, actor: User | None = None
    ) -> None:
        """Delete a knowledge base; one that holds documents only with `cascade`.

        Refused by default, because deleting a knowledge base deletes what the
        agents answer from, and a person should say so rather than discover it.
        """
        base = await self._bases.require_by_id(knowledge_base_id)
        held = await self._documents.count_in_knowledge_base(base.id)
        if held and not cascade:
            raise ConflictError(
                f"That knowledge base holds {held} documents. Delete them first, "
                "or delete it with cascade=true.",
                error_code="knowledge_base_not_empty",
                details={"documents": held},
            )
        page = await self._documents.page(limit=MAX_DOCUMENTS_PER_DELETE, knowledge_base_id=base.id)
        for document in page.items:
            await self.delete_document(document.id, actor=actor)
        if page.next_cursor is not None:
            raise ConflictError(
                "That knowledge base holds too many documents to delete at once. "
                "Run the delete again to continue.",
                error_code="knowledge_base_delete_incomplete",
            )
        await self._session.delete(base)
        await self._session.flush()
        self._audit.record(
            AuditAction.KNOWLEDGE_BASE_DELETED,
            actor=actor,
            target_type="knowledge_base",
            target_id=knowledge_base_id,
            target_label=base.name,
            meta={"documents": held},
        )

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
        if outstanding is not None and outstanding.trigger == TRIGGER_SHADOW:
            if only_after_failure:
                return await self._generations.latest(document_id=document.id)
            # A switch's shadow holds the one in-flight slot; a person asking
            # for a re-index of the served version comes first (RQ-25). The
            # builder makes the shadow again once this one publishes.
            outstanding.state = GenerationState.FAILED
            outstanding.claim_token = None
            outstanding.claimed_at = None
            outstanding.next_retry_at = None
            outstanding.last_error_code = SHADOW_PREEMPTED_CODE
            outstanding.error = "Set aside for a re-index of the served version."
            outstanding.last_error_at = datetime.now(UTC)
            await self._session.flush()
            outstanding = None
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
        # Its originals are owed a delete in the same transaction that removes
        # the rows naming them (RQ-23), through the purge worker's ledger.
        await _owe_deletes(
            self._session,
            tenant_id=self._tenant_id,
            keys=await self._generations.original_keys(document_id=document.id),
        )
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
            if generation.trigger != TRIGGER_SHADOW:
                latest[generation.document_id] = generation
            if generation.state is GenerationState.ACTIVE:
                active[generation.document_id] = generation
        # Stale against the space this workspace is queried in, which is the
        # deployment's own until the workspace has switched (RQ-25).
        space = (
            await EmbeddingSpaceRepository(self._session, tenant_id=self._tenant_id).query_space(
                self._space
            )
            if self._space is not None
            else None
        )
        return [
            DocumentView(
                document=document,
                latest=latest.get(document.id),
                active=active.get(document.id),
                needs_reindex=_is_stale(active.get(document.id), space),
            )
            for document in documents
        ]


def _is_stale(generation: DocumentIndexGeneration | None, space: EmbeddingSpace | None) -> bool:
    if generation is None or space is None:
        return False
    return (
        generation.embedding_provider,
        generation.embedding_model,
        generation.embedding_dimensions,
        generation.embedding_schema_version,
    ) != (space.provider, space.model, space.dimensions, space.schema_version)


__all__ = [
    "DEFAULT_KNOWLEDGE_BASE_NAME",
    "MAX_DOCUMENT_CHARACTERS",
    "DocumentView",
    "Extracted",
    "IngestionResult",
    "KnowledgeService",
    "content_hash",
    "extract",
]
