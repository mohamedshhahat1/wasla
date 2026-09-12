"""The sweep that finishes uploads whose ingestion job never reached Redis.

`KnowledgeService._enqueue` logs a `RedisError` and swallows it, and that is the
right call: the document is already committed and already `PENDING`, which is
the truth - it exists and is not yet searchable - and failing the request would
throw away a document the customer successfully uploaded because Redis was busy
for a moment. Its docstring said `list_pending` existed "so a sweeper can find
anything stranded".

There was no sweeper. Nothing in the deployment called `list_pending` for
documents at all, and there was no gauge, no alert and no operator command
either - so a document accepted from a customer could sit `PENDING` for ever,
never searchable by any agent, with nobody told: not the customer, not the
workspace, not an operator (WQ-03).

This is that sweeper, and it is deliberately the plainest one in the package.
`InboundRecoveryWorker` has to *re-derive* what each event still owes, because
replaying a webhook would re-project a message and re-meter a delivery. Nothing
like that applies here: ingestion is idempotent by construction - a document
already `READY` is left alone, and a re-run replaces its chunks rather than
doubling them - so recovering a stranded document is simply publishing its job
again. The only judgement is which documents to touch.

**Two of these running is safe**, and the lock is what makes the sweep legible
rather than what makes it correct. Duplicate jobs would be harmless; two
sweepers republishing an entire backlog during the outage recovery that is
already behind would not be. `FOR UPDATE SKIP LOCKED` divides the work.

**The grace period is not decoration.** A document committed a second ago has
not failed - the request that committed it is very likely publishing its job at
that moment - and claiming it would race the path this exists to back up.

It also closes the other half of `appendfsync everysec`. An unclean host crash
can lose up to a second of Redis writes, including a successfully-published
ingestion job; agent and media work is already covered by
`InboundRecoveryWorker` re-deriving it from `whatsapp_events`, and this is what
covers the third queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.db.session import Database
from app.repositories.knowledge_repository import PendingDocumentSweep
from app.workers.ingestion_queue import IngestionJob, IngestionQueue

logger = get_logger(__name__)

# How long between sweeps. Recovery is not urgent in the way a reply is - a
# document nobody has searched for yet is waiting rather than failing - and a
# tighter loop would query constantly on a deployment where this finds nothing,
# which is every healthy deployment.
POLL_SECONDS = 60.0

# How old a document must be before the sweep will touch it. Comfortably longer
# than an upload request can take, so the sweep never races the path it is
# backing up. Five minutes, matching the inbound sweeper, because an operator
# reading two backlogs should not have to hold two thresholds in their head.
GRACE_SECONDS = 300.0

# How many documents one pass claims. Bounded so a long outage is drained over
# several passes rather than in one transaction holding hundreds of row locks.
BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class IngestionRecoveryOutcome:
    """What one pass did. Returned so a test can assert on it directly."""

    claimed: int = 0
    queued: int = 0
    still_owing: int = 0


class IngestionRecoveryWorker:
    """Re-queues documents that were committed and never handed to a worker."""

    def __init__(
        self,
        *,
        database: Database,
        redis: RedisClient,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        grace_seconds: float = GRACE_SECONDS,
        batch_limit: int = BATCH_LIMIT,
    ) -> None:
        self._database = database
        self._poll_seconds = poll_seconds
        self._grace = timedelta(seconds=grace_seconds)
        self._batch_limit = batch_limit
        self._queue = IngestionQueue(
            redis.client,
            visibility_timeout_seconds=settings.queue_visibility_timeout_seconds,
        )
        self._running = False
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("ingestion_recovery.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A sweep that fails must not kill the loop. The documents it
                # did not reach are still `PENDING`, so the next pass finds
                # them - which is the whole shape of this worker.
                logger.exception("ingestion_recovery.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("ingestion_recovery.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> IngestionRecoveryOutcome:
        """Claim what is still unindexed and publish as much as Redis allows.

        The claim's transaction ends without marking anything, and that is
        correct rather than an omission: the document's own `status` is the
        durable state, and the ingestion worker is what advances it. A sweep
        that marked rows itself would be inventing a second state machine beside
        the one the consumer already owns.

        A document Redis still refuses stays `PENDING` and is claimed again next
        pass, which is exactly what should happen - there is nothing else to do
        with it, and the gauge beside this keeps it visible meanwhile.
        """
        moment = now or datetime.now(UTC)
        queued = still_owing = 0

        async with self._database.session() as session:
            documents = await PendingDocumentSweep(session).claim_pending(
                older_than=moment - self._grace,
                limit=self._batch_limit,
            )
            if not documents:
                return IngestionRecoveryOutcome()

            for document in documents:
                try:
                    await self._queue.enqueue(
                        IngestionJob(tenant_id=document.tenant_id, document_id=document.id)
                    )
                except RedisError:
                    still_owing += 1
                    continue
                queued += 1

        outcome = IngestionRecoveryOutcome(
            claimed=len(documents),
            queued=queued,
            still_owing=still_owing,
        )
        if outcome.claimed:
            logger.warning(
                "ingestion_recovery.swept",
                extra={
                    "event": "ingestion_recovery.swept",
                    "claimed": outcome.claimed,
                    "queued": outcome.queued,
                    "still_owing": outcome.still_owing,
                },
            )
        return outcome


def unindexed_since(now: datetime, *, grace_seconds: float = GRACE_SECONDS) -> datetime:
    """The cutoff the sweep, the gauge and the operator command all use.

    One function rather than three thresholds, for the reason
    `unprocessed_since` exists: an operator alerted on a backlog and an operator
    running the command must be looking at the same set of documents, and two
    independently chosen numbers would make the alert fire on work the sweep
    considers in flight.
    """
    return now - timedelta(seconds=grace_seconds)


__all__ = [
    "BATCH_LIMIT",
    "GRACE_SECONDS",
    "POLL_SECONDS",
    "IngestionRecoveryOutcome",
    "IngestionRecoveryWorker",
    "unindexed_since",
]
