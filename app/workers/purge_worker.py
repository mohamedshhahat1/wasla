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

**Object storage is deleted after the commit, not inside it.** The keys are read
first, the database transaction commits, and the objects go afterwards. The
ordering is deliberate and the failure it chooses is deliberate too: a crash
between them leaves objects with no rows - orphans, which cost storage and
disclose nothing new, and which the media retention sweep's own reconciliation
is the model for. The other ordering would leave rows pointing at objects that
no longer exist, and a workspace that reported itself unpurged while its
customer's files were already gone.

A deployment that has not set `WORKSPACE_DELETION_RETENTION_DAYS` still runs
this loop, and it does nothing until a deleted workspace's stamped deadline
arrives. A deployment that wants no purge at all runs the worker set without
``purge`` in it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import observe_lifecycle_event
from app.db.session import Database
from app.services.workspace_purge_service import (
    WorkspacePurgeService,
    purge_media_objects,
)

logger = get_logger(__name__)

# Daily. A retention period is a date, and sweeping harder would be querying
# constantly to learn nothing.
POLL_SECONDS: Final = 86_400.0
# How many workspaces one pass erases. Small: each one is a couple of dozen
# `DELETE` statements over what may be a large amount of data, and a sweep that
# tried to drain a backlog in one transaction would hold locks for as long as
# the slowest workspace in it.
BATCH_SIZE: Final = 20


@dataclass(frozen=True, slots=True)
class PurgePass:
    """What one sweep did."""

    purged: int
    rows_deleted: int
    objects_deleted: int
    objects_failed: int


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
            try:
                await self.run_once()
            except Exception:
                # A failed sweep must not kill the loop: the rows are still
                # eligible tomorrow, and an unreachable object store is not a
                # reason to stop erasing databases.
                logger.exception("purge.sweep_failed")
                observe_lifecycle_event(operation="workspace_purge", outcome="error")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
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
        purged = rows = objects = failed = 0

        while purged < BATCH_SIZE:
            async with self._database.session() as session:
                service = WorkspacePurgeService(session)
                claimed = await service.claim_due(now=moment, limit=1)
                if not claimed:
                    break
                tenant = claimed[0]
                # Read before the rows go: after the delete there is nothing
                # left to tell us which objects belonged to this workspace.
                keys = await purge_media_objects(session, tenant.id)
                outcome = await service.purge(tenant, now=moment)
                await session.commit()

            purged += 1
            rows += outcome.rows_deleted

            # After the commit. See the module docstring for why this ordering
            # is the one that fails safely.
            for key in keys:
                try:
                    await self._storage.delete(key)
                    objects += 1
                except Exception:
                    # Logged and counted, never raised: an object the store
                    # will not delete is an orphan, and an orphan must not stop
                    # the next workspace from being erased. The key is not
                    # logged - it names a customer's file.
                    failed += 1
                    logger.warning(
                        "purge.object_delete_failed",
                        extra={
                            "event": "purge.object_delete_failed",
                            "tenant_id": str(tenant.id),
                        },
                    )

        if purged:
            logger.info(
                "purge.sweep_completed",
                extra={
                    "event": "purge.sweep_completed",
                    "workspaces": purged,
                    "rows_deleted": rows,
                    "objects_deleted": objects,
                    "objects_failed": failed,
                },
            )
        return PurgePass(
            purged=purged,
            rows_deleted=rows,
            objects_deleted=objects,
            objects_failed=failed,
        )
