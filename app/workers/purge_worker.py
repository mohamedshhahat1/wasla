"""The worker that erases a deleted workspace once its retention has passed.

Time-triggered, so it polls PostgreSQL rather than blocking on a queue, in the
same shape as the follow-up, campaign, billing and media-retention loops
(ADR-022): a workspace becoming eligible is a row whose date has arrived, not a
message somebody pushed.

It sweeps daily. Retention is measured in days, and a workspace that becomes
eligible at 02:00 and is erased at 11:00 has cost nobody anything.

**Not a queue job**, for the reason the media retention worker gives and which
applies more strongly here: enqueueing one job per workspace would put the
erasure of a customer's entire business data behind the replay command, where an
operator could re-run a dead-lettered purge weeks later against a workspace that
had since been restored by hand. The claim in the database is a better record of
intent than a job payload, it survives Redis entirely, and resuming after a
crash is a query rather than a recovery mechanism.

**Object storage is deleted after the commit, not inside it - and the commit
records what it owes.** The purge transaction deletes the media rows and, in the
same statement, writes each key they held to `media_purge_objects`. The objects
are deleted afterwards, and a key's ledger row is removed only when the store
confirms the object is gone. A refused delete, or a process that dies after the
commit, leaves the ledger row, and the next pass tries again.

This replaced keys held in memory between the commit and the deletes, with a
docstring pointing at "the media retention sweep's reconciliation" - which
starts from rows, and the rows were gone. A delete that failed then left the
customer's files in the bucket with nothing anywhere naming them, and the
workspace recorded as purged with its alert green (MEDIA-07). A workspace is now
fully purged when it is marked purged *and* none of its keys remain owed.

A key whose upload was still in flight at the purge is not deleted before the
upload grace period (`MEDIA_UPLOAD_GRACE_SECONDS`), so a write landing after the
purge is deleted rather than orphaned; the writer also removes its own object if
it finds its row gone (`MediaService`).

A deployment that has not set `WORKSPACE_DELETION_RETENTION_DAYS` still runs
this loop, and it does nothing until a deleted workspace's stamped deadline
arrives. A deployment that wants no purge at all runs the worker set without
``purge`` in it.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import observe_lifecycle_event, record_media_purge_objects
from app.db.session import Database
from app.repositories.media_purge_repository import MediaPurgeLedger
from app.services.workspace_purge_service import WorkspacePurgeService

logger = get_logger(__name__)

# Daily. A retention period is a date, and sweeping harder would be querying
# constantly to learn nothing.
POLL_SECONDS: Final = 86_400.0
# How many workspaces one pass erases. Small: each one is a couple of dozen
# `DELETE` statements over what may be a large amount of data, and a sweep that
# tried to drain a backlog in one transaction would hold locks for as long as
# the slowest workspace in it.
BATCH_SIZE: Final = 20
# How many owed object deletes one pass attempts.
DRAIN_BATCH_SIZE: Final = 500
# How soon to look again while deletes are still owed - a refused delete, or a
# key waiting out its in-flight grace - rather than waiting a whole day.
DRAIN_RETRY_SECONDS: Final = 300.0


@dataclass(frozen=True, slots=True)
class PurgePass:
    """What one sweep did."""

    purged: int
    rows_deleted: int
    objects_deleted: int
    objects_failed: int
    # Deletes still owed after this pass: refused ones, and keys still inside
    # their in-flight grace. Zero means every purged workspace's files are gone.
    objects_pending: int = 0


class PurgeWorker:
    """Erases deleted workspaces whose retention window has run out."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        storage: MediaStorage | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        # The same factory the API and the media workers use, so a deployment
        # cannot sweep one store while writing to another - which would delete
        # nothing and report success.
        self._storage = storage or build_media_storage(settings)
        self._poll_seconds = poll_seconds if poll_seconds is not None else POLL_SECONDS
        self._running = False
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info(
            "purge.worker_started",
            extra={
                "event": "purge.worker_started",
                "retention_days": self._settings.workspace_deletion_retention_days,
            },
        )
        while self._running:
            wait = self._poll_seconds
            try:
                outcome = await self.run_once()
                if outcome.objects_pending:
                    wait = min(wait, DRAIN_RETRY_SECONDS)
            except Exception:
                # A failed sweep must not kill the loop: the rows are still
                # eligible tomorrow, and an unreachable object store is not a
                # reason to stop erasing databases.
                logger.exception("purge.sweep_failed")
                observe_lifecycle_event(operation="workspace_purge", outcome="error")
                wait = min(wait, DRAIN_RETRY_SECONDS)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait)
            except TimeoutError:
                continue
        logger.info("purge.worker_stopped", extra={"event": "purge.worker_stopped"})

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> PurgePass:
        """One pass. Returns what it did.

        **One transaction per workspace**, so a backlog drains incrementally and
        a failure on the fourth workspace does not undo the first three. The
        lock taken by `claim_due` lives until each transaction ends, which is
        why the claim happens inside the same block that does the work.
        """
        moment = now or datetime.now(UTC)
        purged = rows = 0

        while purged < BATCH_SIZE:
            async with self._database.session() as session:
                service = WorkspacePurgeService(
                    session,
                    in_flight_grace=timedelta(seconds=self._settings.media_upload_grace_seconds),
                )
                claimed = await service.claim_due(now=moment, limit=1)
                if not claimed:
                    break
                # The media rows and the record of their keys go in this one
                # commit; there is no window in which the keys exist nowhere.
                outcome = await service.purge(claimed[0], now=moment)
                await session.commit()

            purged += 1
            rows += outcome.rows_deleted

        objects, failed, pending = await self._drain(now=moment)

        if purged or objects or failed:
            logger.info(
                "purge.sweep_completed",
                extra={
                    "event": "purge.sweep_completed",
                    "workspaces": purged,
                    "rows_deleted": rows,
                    "objects_deleted": objects,
                    "objects_failed": failed,
                    "objects_pending": pending,
                },
            )
        return PurgePass(
            purged=purged,
            rows_deleted=rows,
            objects_deleted=objects,
            objects_failed=failed,
            objects_pending=pending,
        )

    async def _drain(self, *, now: datetime) -> tuple[int, int, int]:
        """Delete the objects purges still owe. Returns (deleted, failed, still owed).

        No transaction is open across a delete: the due keys are read, the
        connection goes back, the store is asked, and the answers are recorded
        in a second short transaction. A delete that fails leaves its row for
        the next pass. The key is never logged - it names a customer's file.
        """
        async with self._database.session() as session:
            owed = await MediaPurgeLedger(session).due(now=now, limit=DRAIN_BATCH_SIZE)

        deleted: list[uuid.UUID] = []
        refused: list[uuid.UUID] = []
        for entry in owed:
            try:
                await self._storage.delete(entry.storage_key)
                deleted.append(entry.entry_id)
            except Exception:
                refused.append(entry.entry_id)
                logger.warning(
                    "purge.object_delete_failed",
                    extra={"event": "purge.object_delete_failed"},
                )

        async with self._database.session() as session:
            ledger = MediaPurgeLedger(session)
            for entry_id in deleted:
                await ledger.settle(entry_id)
            for entry_id in refused:
                await ledger.refused(entry_id, now=now)
            pending = await ledger.outstanding()
            await session.commit()
        await record_media_purge_objects(deleted=len(deleted), failed=len(refused))
        return len(deleted), len(refused), pending
