"""The sweep that hands outstanding indexing work back to a worker - and only that.

It began as the sweeper for uploads whose ingestion job never reached Redis
(WQ-03): `KnowledgeService._enqueue` logs a `RedisError` and swallows it, because
the document is already committed and failing the request would throw away an
upload over a moment of Redis trouble. Something had to find those.

What it found as well was every document whose ingestion had *failed*, because a
failure was never recorded and every broken document still looked `pending` -
and it re-queued each of them every sixty seconds, for ever, at five provider
calls a time (RAG-01). Recovery meant "anything old and pending".

**It now means "owed an attempt, and allowed one".** The eligibility predicate
is `IndexingSweep.claim_due`, and it admits exactly three things: a pending
generation never retried and older than the grace period (the lost job this was
built for), a pending generation whose backoff has passed, and a processing
generation whose lease has lapsed (a worker that died). Never a failed one;
never one past its attempt budget; never one in a suspended or deleted
workspace (RAG-10). A document that fails permanently is `FAILED` after one
attempt and this sweep never looks at it again; one that fails transiently comes
back a bounded number of times and then is `FAILED` as `retry_exhausted`.

**Two of these running is safe.** `FOR UPDATE SKIP LOCKED` divides the backlog,
and the claim a worker makes admits one worker per generation whatever the queue
holds - a duplicate job costs one short transaction and no provider call.

**The grace period is not decoration.** A document committed a second ago has
not failed - the request that committed it is very likely publishing its job at
that moment - and claiming it would race the path this exists to back up.

It also closes the other half of `appendfsync everysec`: an unclean host crash can
lose up to a second of Redis writes, including a published ingestion job.
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
from app.repositories.knowledge_repository import IndexingSweep
from app.services.document_indexing import CLAIM_LEASE, MAX_INDEXING_ATTEMPTS
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
        """Claim what is owed an attempt and publish as much as Redis allows.

        The claim's transaction ends without marking anything, and that is
        correct rather than an omission: the generation's own state is the
        durable record, and the worker's claim is what advances it. A sweep that
        marked rows itself would be inventing a second state machine beside the
        one the consumer already owns.

        A generation Redis still refuses is claimed again next pass, which is
        exactly what should happen; the gauges keep it visible meanwhile.
        """
        moment = now or datetime.now(UTC)
        queued = still_owing = 0

        async with self._database.session() as session:
            due = await IndexingSweep(session).claim_due(
                now=moment,
                unenqueued_before=moment - self._grace,
                lease_expired_before=moment - CLAIM_LEASE,
                max_attempts=MAX_INDEXING_ATTEMPTS,
                limit=self._batch_limit,
            )
            if not due:
                return IngestionRecoveryOutcome()

            for generation in due:
                try:
                    await self._queue.enqueue(
                        IngestionJob(
                            tenant_id=generation.tenant_id, document_id=generation.document_id
                        )
                    )
                except RedisError:
                    still_owing += 1
                    continue
                queued += 1

        outcome = IngestionRecoveryOutcome(
            claimed=len(due),
            queued=queued,
            still_owing=still_owing,
        )
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
