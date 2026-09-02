"""The worker that removes stored files once they are old enough.

Time-triggered, so it polls PostgreSQL rather than blocking on a queue, in the
same shape as the follow-up, campaign and billing loops (ADR-022): a file
becoming eligible is a row whose date has arrived, not a message somebody
pushed.

It sweeps far less often than any of them - daily, by default. Retention is
measured in days, and a file that becomes eligible at 02:00 and is removed at
11:00 has cost nobody anything.

**Not a queue job, and that is the decision.** Enqueueing one job per file would
put the deletion of customer data behind the replay command, where an operator
could re-run a dead-lettered purge weeks later against a row that has since been
re-populated. The claim in the database is a better record of intent than a job
payload, it survives Redis entirely, and resuming after a crash is a query rather
than a recovery mechanism - which is the same reason the billing sweep is not a
queue either.

A deployment that has not set `MEDIA_RETENTION_DAYS` runs this loop and it does
nothing: `claim` returns immediately on a retention of zero. That is deliberate
over not starting the loop at all, because the reconciliation pass below still
has to run - a deployment that turns retention *off* after a failed sweep would
otherwise strand every row that sweep had claimed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import record_retention_pass
from app.db.session import Database
from app.services.media_retention_service import MediaRetentionService, RetentionOutcome

logger = get_logger(__name__)

# Daily. A retention period is a date, and sweeping harder would be querying
# constantly to learn nothing.
POLL_SECONDS: Final = 86_400.0


class RetentionWorker:
    """Polls for stored files past their retention and removes them."""

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
        # Built from the same factory the API and the media worker use, so a
        # deployment cannot sweep one store while writing to another - which
        # would delete nothing and report success.
        self._storage = storage or build_media_storage(settings)
        self._poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.media_retention_poll_seconds
        )
        self._running = False
        # Set by stop(), so shutdown does not wait out a day.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info(
            "retention.worker_started",
            extra={"retention_days": self._settings.media_retention_days},
        )
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed sweep must not kill the loop. The store being
                # unavailable for a day is not a reason to stop trying, and the
                # claimed rows are still claimed when it comes back.
                logger.exception("retention.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("retention.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> RetentionOutcome:
        """One pass. Returns what it did.

        Reconciliation runs first. A row an earlier pass claimed and could not
        finish is work that is already decided, and finishing it before claiming
        anything new keeps a store that is refusing deletions from accumulating
        an ever-growing set of half-done rows behind an ever-growing set of new
        claims.
        """
        moment = now or datetime.now(UTC)
        batch = self._settings.media_retention_batch_size

        async with self._database.session() as session:
            service = MediaRetentionService(session=session, storage=self._storage)

            resumed = await service.reconcile(limit=batch)
            outcome = await service.sweep(
                now=moment,
                retention_days=self._settings.media_retention_days,
                limit=batch,
            )
            pending = await service.pending_count()

        await record_retention_pass(
            purged=outcome.purged + resumed,
            failed=outcome.failed,
            pending=pending,
        )

        if outcome.claimed or resumed:
            logger.info(
                "retention.sweep_completed",
                extra={
                    "event": "retention.sweep_completed",
                    "claimed": outcome.claimed,
                    "purged": outcome.purged,
                    "resumed": resumed,
                    "failed": outcome.failed,
                    "pending": pending,
                },
            )
        return outcome
