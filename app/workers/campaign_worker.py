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
thousand people the same message twice.

**One connection pool per sweep.** The messaging service is handed an HTTP client
that lives for the whole sweep, so a batch of fifty is fifty requests over one
connection rather than fifty handshakes to the same host.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.campaign import Campaign
from app.db.session import Database
from app.integrations.whatsapp.client import build_http_client
from app.repositories.campaign_repository import (
    DEFAULT_CAMPAIGN_CLAIM_LIMIT,
    DueCampaignClaim,
)
from app.services.campaign_service import BatchOutcome, CampaignService
from app.services.messaging_service import MessagingService

logger = get_logger(__name__)

# How long between sweeps. Shorter than the follow-up worker's, because a
# campaign's own rate limit is expressed per minute and a sweep that arrives
# late makes the campaign slower than the workspace asked for. Not shorter than
# this: an idle deployment should not query constantly.
POLL_SECONDS = 15.0

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
    ) -> None:
        self._database = database
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._claim_limit = claim_limit
        self._messaging_factory = messaging_factory
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

        One session and one HTTP client for the whole sweep, committed at the
        end. The campaign rows stay locked until then, which is what keeps
        another replica off them, so the sweep is bounded by `claim_limit`
        rather than draining every campaign on the platform under one lock.
        """
        moment = now or datetime.now(UTC)
        handled = 0

        async with self._database.session() as session:
            claimed = await DueCampaignClaim(session).claim_due(
                now=moment,
                limit=self._claim_limit,
            )
            if not claimed:
                return 0

            async with build_http_client() as http:
                for campaign in claimed:
                    try:
                        outcome = await self._send_batch(session, campaign, http=http, now=moment)
                    except Exception:
                        # Contained to the one campaign. A single broken row
                        # must not strand every other workspace's sends.
                        logger.exception(
                            "campaign.batch_failed",
                            extra={"campaign_id": str(campaign.id)},
                        )
                        continue
                    handled += 1
                    logger.debug(
                        "campaign.batch_handled",
                        extra={
                            "campaign_id": str(campaign.id),
                            "status": outcome.status.value,
                            "attempted": outcome.attempted,
                        },
                    )

        logger.info("campaign.sweep_completed", extra={"handled": handled})
        return handled

    async def _send_batch(
        self,
        session: AsyncSession,
        campaign: Campaign,
        *,
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
        )
        return await service.dispatch_batch(campaign, now=now)


__all__ = ["POLL_SECONDS", "CampaignWorker"]
