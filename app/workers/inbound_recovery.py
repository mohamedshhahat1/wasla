"""The sweep that finishes inbound events whose handoff never reached a queue.

A webhook that cannot reach Redis still answers `200`, and that is correct: a
non-2xx would make Meta retry the whole delivery and eventually disable the
subscription, so a Redis outage must not become a webhook outage. What was
missing was the other half of that trade. The message was stored and visible in
the inbox, no worker had been told about it, deduplication discarded Meta's
later redelivery without re-queueing anything, and no query, metric or command
could find it afterwards. The runbook told an operator to requeue those
conversations; there was nothing to requeue them with (MSG-02).

This is that mechanism. `whatsapp_events.state` now means something -
`PROCESSED` is "projected *and* every handoff it needed was accepted", not
merely "a row exists" - and this loop claims what is still `RECEIVED` and
finishes it (ADR-102).

**Recovery re-derives; it does not replay.** Replaying a stored webhook as if
it were new would re-project the message, re-cancel follow-ups and re-meter the
delivery. Instead each event is asked what it is still missing - is the message
there, does its file still need reading, does its conversation still need an
agent turn - and only that is done.

**Two of these running is safe.** Rows are claimed with `FOR UPDATE SKIP
LOCKED` and marked in the same transaction that holds the lock, so two sweepers
looking at one event produce one agent turn between them. That matters more
here than anywhere else in this file: the thing being recovered ends in a
message to somebody's customer, and recovering it twice is the duplicate reply
the whole delivery design exists to prevent.

**The residual window is the same one the webhook already accepts.** The job is
published before the transaction commits, so a commit that then fails leaves a
job whose event is still `RECEIVED` and a later sweep will publish a second.
The alternative - commit first, publish after - turns a crash into permanent
silence, which is the failure this worker exists to remove. ADR-089 made that
trade on the inbound path and this follows it rather than inventing a second
answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.db.models.conversation import MessageKind
from app.db.models.media import MediaStatus
from app.db.models.whatsapp import WhatsAppEvent, WhatsAppEventKind
from app.db.session import Database
from app.repositories.conversation_repository import MessageRepository
from app.repositories.media_repository import MediaRepository
from app.repositories.whatsapp_repository import (
    InboundEventSweep,
    WhatsAppAccountRepository,
    WhatsAppEventRepository,
)
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue

logger = get_logger(__name__)

# How long between sweeps. Recovery is not urgent in the way a reply is - the
# outage that caused it is usually minutes long - and a tighter loop would
# query constantly on a deployment where this finds nothing, which is every
# healthy deployment.
POLL_SECONDS = 60.0

# How old an event must be before the sweep will touch it. An event stored a
# second ago has not failed; it is being processed by the request that stored
# it, whose transaction has not committed. Comfortably longer than a webhook
# request can take, so the sweep never races the path it is backing up.
GRACE_SECONDS = 300.0

# How many events one pass claims. Bounded so a long outage is drained over
# several passes rather than in one transaction holding hundreds of row locks.
BATCH_LIMIT = 100

# Why an event was given up on. Bounded tokens, like the reasons ingestion
# writes: this column is printed by the operator command and shipped in logs.
PROJECTION_MISSING = "projection_missing"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What one pass did. Returned so a test can assert on it directly."""

    claimed: int = 0
    agent_jobs: int = 0
    media_jobs: int = 0
    completed: int = 0
    still_owing: int = 0
    abandoned: int = 0


class InboundRecoveryWorker:
    """Finishes stored inbound events that never reached a queue."""

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
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._grace = timedelta(seconds=grace_seconds)
        self._batch_limit = batch_limit
        timeout = settings.queue_visibility_timeout_seconds
        self._agent_queue = AgentQueue(redis.client, visibility_timeout_seconds=timeout)
        self._media_queue = MediaQueue(redis.client, visibility_timeout_seconds=timeout)
        self._running = False
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("inbound_recovery.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A sweep that fails must not kill the loop. The events it did
                # not reach are still `RECEIVED`, so the next pass finds them.
                logger.exception("inbound_recovery.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("inbound_recovery.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> RecoveryOutcome:
        """Claim what is still owed and finish as much of it as Redis allows.

        One transaction for the whole batch, unlike the follow-up sweep. That
        is the right shape here for the opposite reason: nothing in this pass
        talks to a provider, so no lock is held across a network call that
        could stall, and holding the locks until the marks commit is exactly
        what stops a second sweeper duplicating the work.
        """
        moment = now or datetime.now(UTC)
        agent_jobs = media_jobs = completed = still_owing = abandoned = 0

        async with self._database.session() as session:
            events = await InboundEventSweep(session).claim_unprocessed(
                older_than=moment - self._grace,
                limit=self._batch_limit,
            )
            if not events:
                return RecoveryOutcome()

            for event in events:
                resolution = await self._recover(session, event)
                repository = WhatsAppEventRepository(session, tenant_id=event.tenant_id)
                if resolution.abandon is not None:
                    repository.mark_failed(event, reason=resolution.abandon)
                    abandoned += 1
                    continue
                if resolution.owing is not None:
                    repository.mark_unprocessed(event, reason=resolution.owing)
                    still_owing += 1
                    continue
                agent_jobs += resolution.agent_jobs
                media_jobs += resolution.media_jobs
                repository.mark_processed(event)
                completed += 1

        outcome = RecoveryOutcome(
            claimed=len(events),
            agent_jobs=agent_jobs,
            media_jobs=media_jobs,
            completed=completed,
            still_owing=still_owing,
            abandoned=abandoned,
        )
        if outcome.claimed:
            logger.warning(
                "inbound_recovery.swept",
                extra={
                    "event": "inbound_recovery.swept",
                    "claimed": outcome.claimed,
                    "agent_jobs": outcome.agent_jobs,
                    "media_jobs": outcome.media_jobs,
                    "still_owing": outcome.still_owing,
                    "abandoned": outcome.abandoned,
                },
            )
        return outcome

    async def _recover(self, session: AsyncSession, event: WhatsAppEvent) -> _Resolution:
        """Work out what this event is still missing, and supply it.

        Every branch is a question about the current state of the database
        rather than about what the original request did, which is what makes
        running this twice harmless: the second pass finds the work already
        done and completes the event without repeating it.
        """
        if event.kind is not WhatsAppEventKind.MESSAGE:
            # A status projects onto a message and queues nothing. Reaching
            # here means the request that stored it died before marking it, or
            # that it predates this state machine existing; either way nothing
            # is owed.
            return _Resolution()

        messages = MessageRepository(session, tenant_id=event.tenant_id)
        message = await messages.get_by_wa_message_id(event.event_id)
        if message is None:
            # The projection and the event insert share one transaction, so
            # this should be unreachable. It is recorded rather than repaired
            # because inventing a message row from a stored payload is a
            # guess, and a guess about a customer's words is worse than an
            # event an operator has to look at.
            logger.error(
                "inbound_recovery.projection_missing",
                extra={
                    "event": "inbound_recovery.projection_missing",
                    "whatsapp_event_id": str(event.id),
                },
            )
            return _Resolution(abandon=PROJECTION_MISSING)

        media = await MediaRepository(session, tenant_id=event.tenant_id).get_for_message(
            message.id
        )
        if media is not None:
            if media.status is not MediaStatus.PENDING:
                # The worker has already picked it up, or finished with it.
                return _Resolution()
            try:
                await self._media_queue.enqueue(
                    MediaJob(tenant_id=event.tenant_id, media_id=media.id)
                )
            except RedisError:
                return _Resolution(owing="media_enqueue_failed")
            return _Resolution(media_jobs=1)

        if message.kind is MessageKind.UNSUPPORTED:
            # Nothing to answer. Enqueueing a turn here is the cost MSG-19
            # describes: one billed inference told a customer sent
            # `[unsupported]`, answering a message with no content.
            return _Resolution()

        account = await WhatsAppAccountRepository(session, tenant_id=event.tenant_id).get_by_id(
            event.account_id
        )
        if account is None or account.released_at is not None:
            # The workspace no longer holds this number, so a reply could not
            # leave the building. Recorded, not answered.
            return _Resolution()

        try:
            await self._agent_queue.enqueue(
                AgentJob(tenant_id=event.tenant_id, conversation_id=message.conversation_id)
            )
        except RedisError:
            return _Resolution(owing="agent_enqueue_failed")
        return _Resolution(agent_jobs=1)


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What recovering one event produced.

    `owing` means Redis is still refusing and the event must stay claimable.
    `abandon` means no future pass will do better, so the event stops being
    swept and starts being an operator's problem - which is a smaller problem
    than an event swept for ever.
    """

    agent_jobs: int = 0
    media_jobs: int = 0
    owing: str | None = None
    abandon: str | None = None


def unprocessed_since(now: datetime, *, grace_seconds: float = GRACE_SECONDS) -> datetime:
    """The cutoff both the sweep and the gauge use, so they agree.

    An operator alerted on a backlog and an operator running the sweep must be
    looking at the same set of events; two independently chosen thresholds
    would make the alert fire on work the sweep considers in flight.
    """
    return now - timedelta(seconds=grace_seconds)


__all__ = [
    "BATCH_LIMIT",
    "GRACE_SECONDS",
    "POLL_SECONDS",
    "InboundRecoveryWorker",
    "RecoveryOutcome",
    "unprocessed_since",
]
