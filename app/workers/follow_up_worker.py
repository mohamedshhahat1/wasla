"""The worker that sends scheduled follow-ups when they come due.

Unlike the agent and ingestion workers this one has no queue to block on. A
follow-up is *time*-triggered, and its due moment may be days after it was
scheduled, so there is nothing to push and nothing to wait for. It polls
instead: every interval it asks the database for pending rows whose time has
arrived (ADR-022).

Two consequences follow, and both are deliberate.

**Precision is bounded by the interval.** A follow-up fires within one poll of
its due time, not at it. For a nudge measured in half-hours that is
indistinguishable from exact, and buying exactness would mean a scheduler
holding state that a restart could lose.

**Concurrency is settled by the database.** Rows are claimed with ``FOR UPDATE
SKIP LOCKED``, so a second replica sweeping at the same instant steps over what
the first has taken rather than sending the customer the same message twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.repositories.follow_up_repository import DEFAULT_CLAIM_LIMIT, DueFollowUpClaim
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService

logger = get_logger(__name__)

# How long between sweeps. Short enough that a half-hour nudge is punctual
# enough, long enough that an idle deployment is not querying constantly.
POLL_SECONDS = 30.0

# How the worker obtains something to send with. Injected rather than built
# inline so a test can drive the sweep without a WhatsApp account, the same way
# AgentWorker takes its tool registry.
MessagingFactory = Callable[[AsyncSession, uuid.UUID], MessagingService]


class FollowUpWorker:
    """Polls for due follow-ups and sends them."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        claim_limit: int = DEFAULT_CLAIM_LIMIT,
        messaging_factory: MessagingFactory | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._claim_limit = claim_limit
        self._messaging_factory = messaging_factory
        self._running = False
        # Set when stop() is called, so a sleeping worker wakes at once instead
        # of finishing its interval. Without it, shutdown waits a full poll.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("follow_up.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A sweep that fails must not kill the loop: every later
                # follow-up would go unsent, and nothing would say why.
                logger.exception("follow_up.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("follow_up.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Send every follow-up currently due. Returns how many were handled.

        One session for the whole sweep, committed at the end. The rows stay
        locked until then, which is what keeps another replica off them, so the
        sweep is deliberately bounded by `claim_limit` rather than draining the
        whole backlog under one lock.
        """
        moment = now or datetime.now(UTC)
        handled = 0

        async with self._database.session() as session:
            claimed = await DueFollowUpClaim(session).claim_due(
                now=moment,
                limit=self._claim_limit,
            )
            if not claimed:
                return 0

            for follow_up in claimed:
                # Scoped to the row's own workspace, never to an ambient one:
                # the sweep crosses tenants and the service must not.
                service = FollowUpService(
                    session=session,
                    tenant_id=follow_up.tenant_id,
                    settings=self._settings,
                    messaging=(
                        self._messaging_factory(session, follow_up.tenant_id)
                        if self._messaging_factory is not None
                        else None
                    ),
                )
                try:
                    outcome = await service.dispatch(follow_up)
                except Exception:
                    # Contained to the one follow-up. A single broken row must
                    # not strand every other workspace's nudges behind it.
                    logger.exception(
                        "follow_up.dispatch_failed",
                        extra={"follow_up_id": str(follow_up.id)},
                    )
                    continue
                handled += 1
                logger.debug(
                    "follow_up.dispatched",
                    extra={
                        "follow_up_id": str(follow_up.id),
                        "outcome": outcome.status.value,
                    },
                )

        logger.info("follow_up.sweep_completed", extra={"handled": handled})
        return handled
