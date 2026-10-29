"""Repairing knowledge stored before normalisation and the structure-aware chunker.

Two operator commands, run once at the first deploy of the RAG remediation and
safe to run again (RAG_SYSTEM_REVIEW.md §10, the data-repair warning):

    python -m app.workers.queues backfill-normalised-content [--dry-run]
    python -m app.workers.queues reindex-all [--workspace ID] [--source pdf] --rate 5

**The back-fill** rewrites `documents.content` as `for_storage` text and
recomputes `content_hash`, one document per short transaction, keyset-paged by
id. It is a command, not a migration, so no transaction is ever held across the
whole table. A document whose normalised text another document of the same
knowledge base already holds is a **collision**: nothing is merged or deleted;
the older document is the one to keep, both ids are printed, and the operator
deletes the newer one and runs the back-fill again. A PDF's stored text is
re-checked for re-ordered lines; those are recorded as extraction warnings and
listed, because the stored text cannot repair them - the file has to be
uploaded again.

**`reindex-all`** asks for a new generation of every document through the
normal path - coalesced, fenced, and serving the old generation until the new
one publishes - at a bounded rate, in id order, printing the cursor to resume
from. A document with nothing serving and a failed latest attempt is skipped
and listed: re-trying it is a decision about that document, not part of a
sweep. Only served workspaces are touched; a suspended one spends nothing.

`EMBEDDING_SCHEMA_VERSION` is not changed by either (RQ-02).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import Document, DocumentSource, DocumentStatus
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.repositories.knowledge_repository import TRIGGER_REINDEX
from app.services.pdf_structure import warnings_for
from app.services.text_normalisation import for_storage
from app.workers.ingestion_queue import IngestionQueue

logger = get_logger(__name__)

BATCH: int = 100


@dataclass
class BackfillReport:
    """What one back-fill run changed and what it left for the operator."""

    examined: int = 0
    updated: int = 0
    # (keep, delete): the older document holds the text; delete the newer.
    collisions: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    reupload: list[uuid.UUID] = field(default_factory=list)


@dataclass
class ReindexReport:
    """What one `reindex-all` run asked for, skipped, and where to resume."""

    requested: int = 0
    skipped_failed: list[uuid.UUID] = field(default_factory=list)
    next_after: uuid.UUID | None = None


def content_digest(text: str) -> str:
    from app.services.knowledge_service import content_hash

    return content_hash(text)


async def backfill_normalised_content(
    database: Database, *, batch: int = BATCH, dry_run: bool = False
) -> BackfillReport:
    """Store every document's text as `for_storage` text with its true hash."""
    report = BackfillReport()
    after: uuid.UUID | None = None
    while True:
        async with database.session() as session:
            query = select(Document.id).order_by(Document.id).limit(batch)
            if after is not None:
                query = query.where(Document.id > after)
            ids = list(await session.scalars(query))
        if not ids:
            return report
        for document_id in ids:
            await _backfill_one(database, document_id, report, dry_run=dry_run)
        after = ids[-1]


async def _backfill_one(
    database: Database, document_id: uuid.UUID, report: BackfillReport, *, dry_run: bool
) -> None:
    async with database.session() as session:
        document = await session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )
        if document is None or document.content is None:
            return
        report.examined += 1
        text = for_storage(document.content)
        digest = content_digest(text)
        warnings = warnings_for(text) if document.source is DocumentSource.PDF else {}
        if warnings and not document.extraction_warnings:
            report.reupload.append(document.id)
        if text == document.content and digest == document.content_hash:
            if warnings and not document.extraction_warnings and not dry_run:
                document.extraction_warnings = warnings
            return
        holder = await session.scalar(
            select(Document).where(
                Document.tenant_id == document.tenant_id,
                Document.knowledge_base_id == document.knowledge_base_id,
                Document.content_hash == digest,
                Document.id != document.id,
            )
        )
        if holder is not None:
            keep, drop = sorted((holder, document), key=lambda d: (d.created_at, d.id))
            report.collisions.append((keep.id, drop.id))
            logger.warning(
                "knowledge.backfill_collision",
                extra={
                    "tenant_id": str(document.tenant_id),
                    "keep": str(keep.id),
                    "delete": str(drop.id),
                },
            )
            return
        report.updated += 1
        if dry_run:
            await session.rollback()
            return
        document.content = text
        document.content_hash = digest
        if warnings:
            document.extraction_warnings = warnings


async def reindex_all(
    database: Database,
    queue: IngestionQueue,
    *,
    workspace: uuid.UUID | None = None,
    source: DocumentSource | None = None,
    rate: float = 5.0,
    after: uuid.UUID | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ReindexReport:
    """Ask for a re-index of every document, at most `rate` a second."""
    from app.services.knowledge_service import KnowledgeService

    report = ReindexReport(next_after=after)
    interval = 1.0 / rate if rate > 0 else 0.0
    while limit is None or report.requested + len(report.skipped_failed) < limit:
        async with database.session() as session:
            query = (
                select(Document.id, Document.tenant_id, Document.status)
                .join(Tenant, Tenant.id == Document.tenant_id)
                .where(Tenant.status == TenantStatus.ACTIVE, Tenant.deleted_at.is_(None))
                .order_by(Document.id)
                .limit(BATCH)
            )
            if report.next_after is not None:
                query = query.where(Document.id > report.next_after)
            if workspace is not None:
                query = query.where(Document.tenant_id == workspace)
            if source is not None:
                query = query.where(Document.source == source)
            rows = (await session.execute(query)).all()
        if not rows:
            report.next_after = None
            return report
        for document_id, tenant_id, status in rows:
            if limit is not None and report.requested + len(report.skipped_failed) >= limit:
                return report
            report.next_after = document_id
            if status is DocumentStatus.FAILED:
                report.skipped_failed.append(document_id)
                continue
            report.requested += 1
            if dry_run:
                continue
            async with database.session() as session:
                await KnowledgeService(session=session, tenant_id=tenant_id, queue=queue).reindex(
                    document_id, trigger=TRIGGER_REINDEX
                )
            if interval:
                await pause(interval)
    return report


__all__ = [
    "BackfillReport",
    "ReindexReport",
    "backfill_normalised_content",
    "reindex_all",
]
