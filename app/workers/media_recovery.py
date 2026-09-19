"""The loop that finishes attached files nobody else is going to finish.

Time-triggered and polling PostgreSQL, like upload reconciliation and for the
same reason (ADR-087): the thing it looks for is a row nobody came back to, and
the process that would have pushed a job about it is the one that died.

Run beside the media worker, under the same `media` kind, because it is that
worker's own recovery. A deployment running media workers runs this; one that
does not has no media to recover. Several replicas running it at once is safe:
rows are taken with `SKIP LOCKED`, so they divide the work.

Commit, then enqueue - never the reverse (ADR-092). An enqueue that fails after
the commit costs nothing permanent: a requeued file is still unclaimed and the
next pass finds it again; a released conversation's turn was owed durably in the
transaction that released it (`AgentTurnRepository.owe`), and this loop
republishes every owed turn no agent worker has taken up within the release
horizon - whichever release point owed it, the worker's or this sweep's.
Republishing is safe to repeat: the turn is keyed on its trigger message, so
any number of envelopes for it are one turn and one reply.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import record_media_recovery
from app.db.session import Database
from app.repositories.agent_turn_repository import OwedReleaseSweep
from app.services.media_horizons import release_horizon
from app.services.media_recovery_service import MediaRecoveryPass, MediaRecoveryService
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentJob, AgentQueue

logger = get_logger(__name__)

# Once a minute. The horizons it applies are measured in minutes, so looking
# more often finds nothing new, and looking less often lets a stranded
# conversation wait longer than it has to.
POLL_SECONDS: Final = 60.0


class MediaRecoveryWorker:
    """Polls for stranded media and finishes it."""

    def __init__(
        self,
        *,
        database: Database,
        redis: RedisClient,
        settings: Settings,
        storage: MediaStorage | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._database = database
        self._settings = settings
        self._storage = storage or build_media_storage(settings)
        self._media_queue = MediaQueue(redis.client)
        self._agents = AgentQueue(redis.client)
        self._poll_seconds = poll_seconds
        self._running = False
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        self._running = True
        self._stopping.clear()
        logger.info("media_recovery.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed pass must not kill the loop: this is the loop that
                # exists to survive other things failing.
                logger.exception("media_recovery.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("media_recovery.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> MediaRecoveryPass:
        """One pass: decide and commit, then enqueue what was decided."""
        moment = now or datetime.now(UTC)
        async with self._database.session() as session:
            outcome = await MediaRecoveryService(
                session, settings=self._settings, storage=self._storage
            ).sweep(now=moment)
            await session.commit()

        release_failed = 0
        for job in outcome.requeued:
            try:
                await self._media_queue.enqueue(job)
            except Exception:
                # The row is unclaimed and was re-stamped; the next pass after
                # the horizon finds it again and tries once more.
                logger.warning(
                    "media_recovery.requeue_failed",
                    extra={"event": "media_recovery.requeue_failed", "media_id": str(job.media_id)},
                )
        for release in outcome.releases:
            if not await self._publish(release):
                release_failed += 1

        release_recovered, republish_failed = await self._republish_owed(moment)
        await record_media_recovery(
            requeued=len(outcome.requeued),
            abandoned=outcome.abandoned,
            release_failed=release_failed + republish_failed,
            release_recovered=release_recovered,
        )
        return outcome

    async def _republish_owed(self, moment: datetime) -> tuple[int, int]:
        """Publish again every owed turn no agent worker has taken up.

        Its own transaction, committed before anything is published, for the
        same reason as the sweep above: the stamp is what stops the next pass
        publishing the same turn while this job is still queued, and a process
        that dies between the commit and the publish leaves the turn owed and
        stamped, found again one horizon later.
        """
        async with self._database.session() as session:
            owed = await OwedReleaseSweep(session).claim_owed(
                published_before=moment - release_horizon(self._settings), now=moment
            )
            jobs = [
                AgentJob(
                    tenant_id=turn.tenant_id,
                    conversation_id=turn.conversation_id,
                    trigger_message_id=turn.trigger_message_id,
                )
                for turn in owed
            ]
            await session.commit()

        recovered = failed = 0
        for job in jobs:
            if await self._publish(job):
                recovered += 1
            else:
                failed += 1
        if jobs:
            logger.warning(
                "media_recovery.releases_republished",
                extra={
                    "event": "media_recovery.releases_republished",
                    "republished": recovered,
                    "failed": failed,
                },
            )
        return recovered, failed

    async def _publish(self, job: AgentJob) -> bool:
        """Queue one owed turn; a refusal leaves it owed, not lost."""
        try:
            await self._agents.enqueue(job)
        except Exception:
            logger.error(
                "media_recovery.release_failed",
                extra={
                    "event": "media_recovery.release_failed",
                    "conversation_id": str(job.conversation_id),
                },
            )
            return False
        return True


__all__ = ["POLL_SECONDS", "MediaRecoveryWorker"]
