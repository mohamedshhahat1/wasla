"""The worker that sends campaigns, a batch at a time.

Like the follow-up worker and unlike the agent one, this polls rather than
consuming a queue. A campaign is time-triggered — it may be scheduled for next
Tuesday — and it is *repeatedly* triggered: one campaign becomes many batches
spread over minutes or hours, so there is nothing to push once and be done with.

Three properties follow from that, and all three are deliberate.

**The rate limit lives in the database, not in this process.** A sweep sends
what the campaign is allowed and writes down when it may send again. A sleep
here would hold a lock, would not survive a restart, and would let two replicas
each sleep their own way to twice the intended rate.

**Concurrency is settled by PostgreSQL.** Campaigns are claimed with ``FOR
UPDATE SKIP LOCKED``, and so are the recipients inside them. Two replicas
sweeping at the same instant step over each other's work rather than sending ten
thousand people the same message twice. The claim also pushes ``next_send_at``
out and commits it, because a row lock ends where its transaction does and a
send now commits part-way through (ADR-093).

**One connection pool per sweep.** The messaging service is handed an HTTP client
that lives for the whole sweep, so a batch of fifty is fifty requests over one
connection rather than fifty handshakes to the same host.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage, build_media_storage
from app.core.telemetry import record_campaign_stop
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.campaign import Campaign, CampaignStopReason
from app.db.session import Database
from app.integrations.whatsapp.client import build_http_client
from app.repositories.campaign_repository import (
    DEFAULT_CAMPAIGN_CLAIM_LIMIT,
    DueCampaignClaim,
)
from app.services.audit_service import AuditTrail
from app.services.campaign_service import BatchOutcome, CampaignLease, CampaignService
from app.services.messaging_service import MessagingService

logger = get_logger(__name__)

# How long between sweeps. Shorter than the follow-up worker's, because a
# campaign's own rate limit is expressed per minute and a sweep that arrives
# late makes the campaign slower than the workspace asked for. Not shorter than
# this: an idle deployment should not query constantly.
POLL_SECONDS = 15.0

# How long a claimed campaign is left alone before the sweep may take it again.
# Short enough that a worker killed mid-batch does not stall the campaign for an
# hour. `dispatch_batch` overwrites it with the rate limiter's own figure when
# the batch finishes, so this value only ever applies to a batch in flight.
#
# **Renewed before every recipient** (CMP-01, ADR-134), so it must outlast one
# send, not a batch. The worst single send the client allows: three attempts of
# at most 10 s each (`REQUEST_TIMEOUT_SECONDS`) and two waits of at most 30 s
# `Retry-After` plus 50 % jitter (`MAX_RETRY_AFTER_SECONDS`, `RETRY_JITTER`) -
# 3 x 10 + 2 x 45 = 120 s. Three hundred is two and a half times that. A lease
# that does run out - a stalled process - is re-taken with a new token, and the
# stalled worker stops at its next recipient; the per-recipient claim keeps the
# one in flight from being sent twice.
CLAIM_LEASE_SECONDS = 300.0

# What a campaign whose batch raised records in `last_error`: a fixed sentence,
# never the exception's text, which can carry anything (CMP-20).
BATCH_FAILED = "A batch of this campaign could not run; it is retried at the next sweep."

# How the worker obtains something to send with. Injected rather than built
# inline so a test can drive a sweep without a WhatsApp account, exactly as
# FollowUpWorker does.
MessagingFactory = Callable[[AsyncSession, uuid.UUID], MessagingService]


class CampaignWorker:
    """Polls for campaigns that should be sending and sends their next batch."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        claim_limit: int = DEFAULT_CAMPAIGN_CLAIM_LIMIT,
        messaging_factory: MessagingFactory | None = None,
        lease_seconds: float = CLAIM_LEASE_SECONDS,
        storage: MediaStorage | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._claim_limit = claim_limit
        self._lease = timedelta(seconds=lease_seconds)
        self._messaging_factory = messaging_factory
        # Where campaign header files are read from before their upload to
        # Meta (HM-04); one store for the worker's life.
        self._storage = storage if storage is not None else build_media_storage(settings)
        self._running = False
        # Set when stop() is called, so a sleeping worker wakes at once instead
        # of finishing its interval.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("campaign.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A sweep that fails must not kill the loop: every campaign
                # after it would stall with nothing to say why.
                logger.exception("campaign.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("campaign.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Send one batch for every campaign currently due. Returns how many.

        Two phases, and the split is a correctness requirement (ADR-093). A
        send commits its intent before Meta can deliver anything, and a commit
        ends the transaction the claim's row lock lives in - so a sweep holding
        one lock across every campaign would drop them all at the first send.

            TX1  claim the due campaigns and push `next_send_at` out by a
                 lease                                              -> COMMIT
            --   the lock is gone; the lease is what keeps others off
            TXn  one transaction per campaign

        `next_send_at` is already the rate limiter's own state and already the
        column `claim_due` filters on, so the lease is the existing mechanism
        used a moment earlier rather than a new one. `dispatch_batch` sets it
        again from its own clock when the batch finishes.
        """
        moment = now or datetime.now(UTC)
        handled = 0

        async with self._database.session() as session:
            claimed = await DueCampaignClaim(session).claim_due(
                now=moment,
                limit=self._claim_limit,
                lease_until=moment + self._lease,
            )
            # Each row's own claim token travels with its id (CMP-01): the
            # batch proceeds only while the row still carries it.
            leases = [(row.id, row.lease_token) for row in claimed]

        if not leases:
            return 0

        started = perf_counter()
        async with build_http_client() as http:
            for campaign_id, token in leases:
                if token is None:  # pragma: no cover - claim_due always writes one
                    continue
                # The sweep's clock, advanced by the time earlier campaigns of
                # this sweep took, so the lease renewal below is measured from
                # now rather than from the start of a long sweep.
                at = moment + timedelta(seconds=perf_counter() - started)
                if await self._send_one(
                    campaign_id,
                    lease=CampaignLease(token=token, duration=self._lease),
                    http=http,
                    now=at,
                ):
                    handled += 1

        logger.info("campaign.sweep_completed", extra={"handled": handled})
        return handled

    async def _send_one(
        self,
        campaign_id: uuid.UUID,
        *,
        lease: CampaignLease,
        http: httpx.AsyncClient,
        now: datetime,
    ) -> bool:
        """One campaign's batch, in a transaction of its own. Returns whether it ran.

        Re-read under a row lock rather than carried over from the claim: that
        transaction has committed, so its object is a snapshot and the campaign
        may have been paused or cancelled since. Re-read **only while the row
        still carries this sweep's claim** (CMP-01): a lease that ran out while
        an earlier campaign of the sweep was sending may have been re-taken by
        another replica, and the campaign is then that replica's.
        """
        failed = False
        try:
            async with self._database.session() as session:
                campaign = await DueCampaignClaim(session).claim_by_id(
                    campaign_id, lease_token=lease.token, lease_until=now + lease.duration
                )
                if campaign is None:
                    return False
                try:
                    outcome = await self._send_batch(
                        session, campaign, lease=lease, http=http, now=now
                    )
                except Exception as error:
                    # Contained to the one campaign. A single broken row must
                    # not strand every other workspace's sends. What was
                    # flushed before it - a recipient's outcome after a send -
                    # still commits with the session, as it always did.
                    failed = True
                    logger.exception(
                        "campaign.batch_failed",
                        extra={
                            "event": "campaign.batch_failed",
                            "campaign_id": str(campaign_id),
                            "error_type": type(error).__name__,
                        },
                    )
        except Exception as error:
            # The batch's own commit failed: nothing of it is durable.
            failed = True
            logger.exception(
                "campaign.batch_failed",
                extra={
                    "event": "campaign.batch_failed",
                    "campaign_id": str(campaign_id),
                    "error_type": type(error).__name__,
                },
            )
        if failed:
            await self._record_batch_failure(campaign_id, lease=lease)
            return False

        logger.debug(
            "campaign.batch_handled",
            extra={
                "campaign_id": str(campaign_id),
                "status": outcome.status.value,
                "attempted": outcome.attempted,
            },
        )
        return True

    async def _record_batch_failure(self, campaign_id: uuid.UUID, *, lease: CampaignLease) -> None:
        """Count a batch that raised, and pause the campaign at the bound (CMP-20).

        In a transaction of its own, after the batch's has ended. A campaign
        whose every batch raised used to keep its lease and be retried every
        five minutes for ever, with no `last_error` and no alert. Now each
        failure is counted and written down as a fixed sentence - never the
        exception's text - and once `CAMPAIGN_BATCH_FAILURE_PAUSE_AFTER` batches
        in a row have failed it is paused `repeated_batch_failure`, for a
        person to look at. Only while this worker still holds the claim.
        """
        try:
            async with self._database.session() as session:
                paused = await DueCampaignClaim(session).record_batch_failure(
                    campaign_id,
                    lease_token=lease.token,
                    detail=BATCH_FAILED,
                    pause_after=self._settings.campaign_batch_failure_pause_after,
                    reason=CampaignStopReason.REPEATED_BATCH_FAILURE.value,
                )
                if paused is not None:
                    AuditTrail(session, tenant_id=paused.tenant_id).record(
                        AuditAction.CAMPAIGN_PAUSED,
                        actor=None,
                        actor_kind=AuditActorKind.SYSTEM,
                        target_type="campaign",
                        target_id=paused.id,
                        target_label=paused.name,
                        meta={"reason": CampaignStopReason.REPEATED_BATCH_FAILURE.value},
                    )
            if paused is not None:
                await record_campaign_stop(CampaignStopReason.REPEATED_BATCH_FAILURE.value)
                logger.error(
                    "campaign.paused_repeated_failure",
                    extra={
                        "event": "campaign.paused_repeated_failure",
                        "campaign_id": str(campaign_id),
                    },
                )
        except Exception:
            logger.exception(
                "campaign.batch_failure_unrecorded", extra={"campaign_id": str(campaign_id)}
            )

    async def _send_batch(
        self,
        session: AsyncSession,
        campaign: Campaign,
        *,
        lease: CampaignLease,
        http: httpx.AsyncClient,
        now: datetime,
    ) -> BatchOutcome:
        """One campaign's batch, scoped to that campaign's own workspace.

        The sweep crosses tenants; the service must not. Both the messaging
        service and the campaign service are built from the row's own
        `tenant_id`, never from an ambient one.
        """
        messaging = (
            self._messaging_factory(session, campaign.tenant_id)
            if self._messaging_factory is not None
            else MessagingService(
                session=session,
                settings=self._settings,
                tenant_id=campaign.tenant_id,
                http=http,
            )
        )
        service = CampaignService(
            session=session,
            tenant_id=campaign.tenant_id,
            messaging=messaging,
            default_plan_code=self._settings.default_plan_code,
            provider_unreachable_pause=timedelta(
                hours=self._settings.campaign_provider_unreachable_pause_hours
            ),
            storage=self._storage,
            header_media_refresh=timedelta(
                seconds=self._settings.campaign_header_media_refresh_seconds
            ),
        )
        return await service.dispatch_batch(campaign, now=now, lease=lease)


__all__ = ["POLL_SECONDS", "CampaignWorker"]
