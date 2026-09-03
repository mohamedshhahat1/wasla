"""The worker that finishes object writes nobody came back to.

Time-triggered, so it polls PostgreSQL rather than blocking on a queue, in the
same shape as the follow-up, campaign, billing and retention loops (ADR-022).
The work it looks for is a row whose state has not changed for a while, which
is not something anybody can push onto a queue: the process that would have
pushed it is the one that died.

**Not a queue job, and for a sharper reason than retention's.** A job naming an
upload to reconcile would itself be lost by exactly the failure it exists to
recover from - a worker killed between the object write and the finalisation is
also a worker killed before it could enqueue anything. The durable intent in
PostgreSQL is the only record that survives that, so the only honest recovery
mechanism is a query over it (ADR-087).

## Why this is not the retention loop

They are neighbours - both sweep `message_media`, both reconcile something an
earlier pass left half-done - and they were nearly one loop. Two things kept
them apart.

The periods differ by two orders of magnitude. Retention is measured in days
and sweeps daily; an unfinished upload is an attachment a colleague cannot open
and an agent cannot read, so it is measured in minutes. One loop would have to
run at the faster rate and skip most of its own work, or at the slower one and
leave media invisible for a day.

And they move in opposite directions. Retention removes objects that exist;
this one adopts objects that may. Sharing a pass would put "delete the file"
and "the file is fine, keep it" behind one decision, which is the kind of
ambiguity that eventually deletes something.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import record_upload_reconciliation
from app.db.session import Database
from app.services.media_upload_service import MediaUploadReconciler, ReconciliationOutcome

logger = get_logger(__name__)

# Every five minutes. An interrupted upload is an attachment that is in the
# store and invisible, so the cost of waiting is a colleague being told a file
# is not there when it is - and the cost of polling is one indexed query
# against a partial index that is normally empty.
POLL_SECONDS: Final = 300.0


class UploadRecoveryWorker:
    """Polls for upload intents whose object write never reported back."""

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
        # Built from the same factory the API, the media worker and retention
        # use, so this cannot verify one store while the writes went to
        # another - which would report every object missing and abandon every
        # intent it found (ADR-077).
        self._storage = storage or build_media_storage(settings)
        self._poll_seconds = (
            poll_seconds
            if poll_seconds is not None
            else settings.media_upload_recovery_poll_seconds
        )
        self._running = False
        # Set by stop(), so shutdown does not wait out a whole period.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Reconcile until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info(
            "upload_recovery.worker_started",
            extra={"grace_seconds": self._settings.media_upload_grace_seconds},
        )
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed pass must not kill the loop. The store being
                # unavailable is the ordinary reason this fails, and the intents
                # are all still there when it comes back.
                logger.exception("upload_recovery.pass_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("upload_recovery.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> ReconciliationOutcome:
        """One pass. Returns what it settled."""
        moment = now or datetime.now(UTC)

        async with self._database.session() as session:
            reconciler = MediaUploadReconciler(session=session, storage=self._storage)
            outcome = await reconciler.run(
                now=moment,
                grace_seconds=self._settings.media_upload_grace_seconds,
                limit=self._settings.media_upload_recovery_batch_size,
            )
            pending = await reconciler.pending_count()
            mismatched = await reconciler.mismatched_count()

        await record_upload_reconciliation(
            finalized=outcome.finalized,
            missing=outcome.missing,
            mismatched=outcome.mismatched,
            unreachable=outcome.unreachable,
            pending=pending,
            quarantined=mismatched,
        )

        if outcome.examined:
            logger.info(
                "upload_recovery.pass_completed",
                extra={
                    "event": "upload_recovery.pass_completed",
                    "finalized": outcome.finalized,
                    "missing": outcome.missing,
                    "mismatched": outcome.mismatched,
                    "unreachable": outcome.unreachable,
                    "pending": pending,
                },
            )
        return outcome


__all__ = ["POLL_SECONDS", "UploadRecoveryWorker"]
