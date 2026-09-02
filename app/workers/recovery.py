"""The loop that picks up work a dead worker was holding.

Until this existed, a worker that stopped between reserving a job and
acknowledging it left that job on the in-flight list for ever. Nothing polled
it, nothing timed it out, and nothing put it back — the job was not lost in the
sense of deleted, it was lost in the sense of never happening again, which is
worse because the queue looked healthy while it happened.

Two passes, and they are the same code:

**At start-up**, once. A container that has just come up is very often the
replacement for one that has just died, so the first useful thing it can do is
look for what the previous one was holding. That does not reclaim anything on
its own — leases have to expire first — but it adopts entries whose reservation
never got written, so the clock starts on them immediately rather than whenever
the first periodic pass happens along.

**Then periodically**, at a third of the visibility timeout, so an expired
lease is noticed within roughly one timeout of expiring rather than one
interval after it.

**Running two of these at once is safe and expected.** Every reclaim goes
through `ReliableQueue._claim_inflight`, whose `LREM` can only succeed for one
caller, so two recovery loops looking at the same expired job produce one
outcome between them and the loser does nothing at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.telemetry import JobOutcome, record_job_outcome
from app.workers.ingestion_queue import IngestionQueue
from app.workers.media_queue import MediaQueue
from app.workers.queue import LEASE_RENEWAL_FRACTION, AgentQueue, ReliableQueue
from app.workers.retry import IDEMPOTENT_RETRY, RetryPolicy

logger = get_logger(__name__)


class RecoveryWorker:
    """Reclaims expired reservations across every queue in this deployment."""

    def __init__(
        self,
        *,
        redis: RedisClient,
        settings: Settings,
        poll_seconds: float | None = None,
    ) -> None:
        timeout = settings.queue_visibility_timeout_seconds
        # Each queue carries the retry policy its own worker uses, so a
        # recovered job is held to the same budget as a job that failed
        # normally. The agent queue is the one that differs, and it differs by
        # being marked non-idempotent rather than by a special case here.
        self._queues: Sequence[tuple[ReliableQueue, RetryPolicy]] = (
            (
                AgentQueue(redis.client, visibility_timeout_seconds=timeout),
                _agent_policy(),
            ),
            (
                IngestionQueue(redis.client, visibility_timeout_seconds=timeout),
                IDEMPOTENT_RETRY,
            ),
            (
                MediaQueue(redis.client, visibility_timeout_seconds=timeout),
                IDEMPOTENT_RETRY,
            ),
        )
        self._poll_seconds = (
            poll_seconds if poll_seconds is not None else timeout * LEASE_RENEWAL_FRACTION
        )
        self._running = False
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop, beginning immediately."""
        self._running = True
        self._stopping.clear()
        logger.info("recovery.worker_started", extra={"poll_seconds": self._poll_seconds})
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed sweep must not kill the loop. This is the loop that
                # exists to survive other things failing, so it had better
                # survive itself.
                logger.exception("recovery.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("recovery.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self) -> int:
        """One pass over every queue. Returns how many reservations were reclaimed."""
        reclaimed = 0
        for queue, policy in self._queues:
            outcomes = await queue.recover_expired(policy=policy)
            for outcome in outcomes:
                reclaimed += 1
                await record_job_outcome(
                    queue=queue.label,
                    outcome=(
                        JobOutcome.RECOVERED
                        if outcome.action == "requeued"
                        else JobOutcome.QUARANTINED
                    ),
                    category=str(outcome.category),
                )
                # `error` rather than `warning` for a quarantine: a requeue is
                # the system healing itself, and a quarantine is a customer
                # who may or may not have been answered, which needs a person.
                log = logger.warning if outcome.action == "requeued" else logger.error
                log(
                    "recovery.reservation_reclaimed",
                    extra={
                        "event": "recovery.reservation_reclaimed",
                        "queue": queue.label,
                        "action": outcome.action,
                        "stage": str(outcome.stage),
                        "category": str(outcome.category),
                        "attempt": outcome.envelope.attempt,
                    },
                )
        return reclaimed


def _agent_policy() -> RetryPolicy:
    """The agent worker's own retry budget, read from where it is defined.

    Imported here rather than at module scope because `ai_worker` imports most
    of the application to do its job, and the recovery loop needs one constant
    from it.
    """
    from app.workers.ai_worker import AGENT_RETRY

    return AGENT_RETRY


__all__ = ["RecoveryWorker"]
