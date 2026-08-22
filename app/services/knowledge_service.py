"""Knowledge base administration and document ingestion.

Two responsibilities that share a module because they share a vocabulary:
managing knowledge bases and documents, and turning a submitted document into
retrievable chunks.

Ingestion is deliberately split from submission. Submitting records the document
and returns; ingesting extracts, chunks, embeds and stores. They are separate
because embedding a document is slow and calls a provider, and no HTTP request -
least of all a Meta webhook - should wait for that (claude.md §61).

Idempotency is by content hash. The same bytes submitted twice to the same
knowledge base are one document, and re-running ingestion over a document
replaces its chunks rather than appending to them, so a retry cannot leave a
document answering with two copies of everything.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass
from typing import Final

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError, WaslaError
from app.core.logging import get_logger
from app.db.models.knowledge import Document, DocumentSource, DocumentStatus, KnowledgeBase
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.knowledge_repository import (
    DocumentChunkRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
)
from app.services import chunking, extraction
from app.workers.ingestion_queue import IngestionJob, IngestionQueue

logger = get_logger(__name__)

DEFAULT_KNOWLEDGE_BASE_NAME: Final = "General"
# Guards the request path. A larger document is not refused on principle, but
# accepting one through a JSON body is the wrong door for it.
MAX_DOCUMENT_CHARACTERS: Final = 400_000


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What one ingestion run concluded."""

    document: Document
    chunks_written: int
    reused: bool


def content_hash(text: str) -> str:
    """SHA-256 of the text, hex encoded.

    Computed over the extracted text rather than the uploaded bytes so that the
    same content submitted as a file and as a paste is recognised as the same
    document.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract(*, raw: str, source: DocumentSource) -> str:
    """Turn a submitted document into plain text.

    Text and Markdown are already text; Markdown keeps its punctuation because
    headings and lists are structure the chunker uses.

    A PDF arrives here base64-encoded, because this endpoint takes JSON and a
    PDF is not text. A scanned one - a photograph of a page, with no text layer -
    is refused rather than stored empty: an empty document looks perfectly
    ingested from the outside and answers every question with nothing, which is
    worse than being told the file needs OCR.
    """
    if source is DocumentSource.PDF:
        try:
            content = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValidationError("Submit a PDF as base64-encoded content.") from error

        text = extraction.extract_pdf(content)
        if not text:
            raise ValidationError(
                "No text could be read from this PDF. It may be a scan, "
                "which needs to be converted to text first."
            )
        return chunking.normalise(text)

    return chunking.normalise(raw)


class KnowledgeService:
    """Knowledge bases and documents for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        queue: IngestionQueue | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        # Optional so the ingestion worker, which is on the other end of this
        # queue, can use the service without holding a handle to it.
        self._queue = queue
        self._bases = KnowledgeBaseRepository(session, tenant_id=tenant_id)
        self._documents = DocumentRepository(session, tenant_id=tenant_id)
        self._chunks = DocumentChunkRepository(session, tenant_id=tenant_id)

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
    ) -> KnowledgeBase:
        base = await self._bases.create(name=name.strip(), description=description)
        await self._session.flush()
        logger.info(
            "knowledge.base_created",
            extra={"tenant_id": str(self._tenant_id), "knowledge_base_id": str(base.id)},
        )
        return base

    async def list_knowledge_bases(self, *, limit: int = 50) -> list[KnowledgeBase]:
        return await self._bases.list_all(limit=limit)

    async def get_knowledge_base(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase:
        return await self._bases.require_by_id(knowledge_base_id)

    async def ensure_default_knowledge_base(self) -> KnowledgeBase:
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
        )

    async def list_documents(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        limit: int = 50,
    ) -> list[Document]:
        # Resolved first so another workspace's id answers not-found rather than
        # an empty list, which would leak that the knowledge base exists.
        await self._bases.require_by_id(knowledge_base_id)
        return await self._documents.list_for_knowledge_base(
            knowledge_base_id=knowledge_base_id,
            limit=limit,
        )

    async def get_document(self, document_id: uuid.UUID) -> Document:
        return await self._documents.require_by_id(document_id)

    async def submit(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        raw: str,
        source: DocumentSource = DocumentSource.TEXT,
        filename: str | None = None,
        media_type: str | None = None,
    ) -> tuple[Document, bool]:
        """Record a document for ingestion. Returns it and whether it is new.

        Nothing is embedded here. The extraction happens because it decides the
        content hash, and the hash is what makes a repeat submission a repeat.
        """
        if len(raw) > MAX_DOCUMENT_CHARACTERS:
            raise ValidationError(
                f"That document is too large. The limit is {MAX_DOCUMENT_CHARACTERS} characters."
            )

        await self._bases.require_by_id(knowledge_base_id)
        text = extract(raw=raw, source=source)
        if not text:
            raise ValidationError("That document has no text to index.")

        document, created = await self._documents.upsert(
            knowledge_base_id=knowledge_base_id,
            title=title.strip(),
            content_hash=content_hash(text),
            source=source,
            filename=filename,
            media_type=media_type,
            byte_size=len(raw.encode("utf-8")),
            content=text,
        )
        await self._session.flush()
        if document.status is DocumentStatus.PENDING:
            await self._enqueue(document.id)
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
        return document, created

    async def reingest(self, document_id: uuid.UUID) -> Document:
        """Queue a document to be indexed again.

        The way a failed ingestion is recovered once its cause is fixed, and the
        way a document is rebuilt after a chunking change. Resets the document
        to pending first, so its status reflects that work is outstanding rather
        than leaving a stale `failed` on a document now waiting in the queue.
        """
        document = await self._documents.require_by_id(document_id)
        document.status = DocumentStatus.PENDING
        document.error = None
        await self._session.flush()
        await self._enqueue(document.id)
        return document

    async def _enqueue(self, document_id: uuid.UUID) -> bool:
        """Ask a worker to index this document. Returns whether it was queued.

        A queue failure is logged and swallowed, not raised. The document is
        already recorded as `pending`, which is the truth: it exists and is not
        yet searchable. Failing the request instead would discard a document the
        customer successfully uploaded because Redis was briefly unavailable,
        and `list_pending` exists so a sweeper can find anything stranded.
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
        """Chunk, embed and store one document.

        Safe to run twice. The chunks are cleared before new ones are written,
        so a retry after a partial failure replaces whatever the failed run left
        rather than doubling it. A document already `READY` is left alone, which
        is what makes a duplicated queue job harmless.

        A failure is recorded on the document and re-raised. Recording it means
        an operator can see which document is broken and why; re-raising lets
        the worker decide whether to retry, and keeps a provider outage from
        looking like a successful ingestion of nothing.
        """
        document = await self._documents.require_by_id(document_id)
        if document.status is DocumentStatus.READY:
            return IngestionResult(document=document, chunks_written=0, reused=True)

        await self._documents.mark_processing(document)
        await self._session.flush()

        try:
            written = await self._rebuild(document, embeddings)
        except WaslaError as error:
            await self._documents.mark_failed(document, reason=str(error))
            await self._session.flush()
            logger.warning(
                "knowledge.ingestion_failed",
                extra={
                    "tenant_id": str(self._tenant_id),
                    "document_id": str(document.id),
                    "reason": type(error).__name__,
                },
            )
            raise

        await self._documents.mark_ready(document, chunk_count=written)
        await self._session.flush()
        logger.info(
            "knowledge.document_ingested",
            extra={
                "tenant_id": str(self._tenant_id),
                "document_id": str(document.id),
                "chunks": written,
            },
        )
        return IngestionResult(document=document, chunks_written=written, reused=False)

    async def _rebuild(self, document: Document, embeddings: EmbeddingsClient) -> int:
        """Replace this document's chunks with freshly embedded ones."""
        if not document.content:
            raise ValidationError("That document has no text to index.")

        pieces = chunking.split(document.content)
        if not pieces:
            # A document that produces no chunks answers nothing, so calling it
            # ready would advertise knowledge that cannot be retrieved.
            raise ValidationError("That document produced no passages worth indexing.")

        vectors = await embeddings.embed([piece.content for piece in pieces])
        if len(vectors) != len(pieces):
            raise ValidationError("The embedding provider returned the wrong number of vectors.")

        await self._chunks.clear_for_document(document_id=document.id)
        for piece, vector in zip(pieces, vectors, strict=True):
            self._chunks.add_chunk(
                document_id=document.id,
                knowledge_base_id=document.knowledge_base_id,
                ordinal=piece.ordinal,
                content=piece.content,
                token_estimate=piece.token_estimate,
                embedding=vector,
            )
        await self._session.flush()
        return len(pieces)

    async def delete_document(self, document_id: uuid.UUID) -> None:
        """Remove a document and everything derived from it.

        A hard delete rather than a soft one. A workspace removing a document
        from its knowledge base is usually removing something that should no
        longer be said to customers, and a soft-deleted row that retrieval
        forgot to filter would keep saying it.
        """
        document = await self._documents.require_by_id(document_id)
        await self._chunks.clear_for_document(document_id=document.id)
        await self._session.delete(document)
        await self._session.flush()
        logger.info(
            "knowledge.document_deleted",
            extra={"tenant_id": str(self._tenant_id), "document_id": str(document_id)},
        )
