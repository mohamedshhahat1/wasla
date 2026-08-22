"""The worker that moves subscriptions on when their period ends.

Time-triggered, like follow-ups and campaigns, so it polls PostgreSQL rather
than blocking on a queue (ADR-022): a period ending is a row whose moment has
arrived, not a message somebody pushed.

It sweeps far less often than the others, and deliberately. Nothing here is
urgent to the minute — a trial that ends at 09:00 and is noticed at 09:55 has
cost nobody anything, because entitlements are computed from the row on every
request and the *row* already says the period is over. What this loop does is
make that state explicit and durable: a trial becomes `expired`, a pending
cancellation takes effect, an active subscription opens its next period.

The rules themselves are in `roll_over`, which is a pure function over a row.
This module is the query, the loop and the commit, and nothing else.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.repositories.billing_repository import (
    PlanRepository,
    PlatformSubscriptionRepository,
)
from app.services.subscription_service import roll_over

logger = get_logger(__name__)

# Ten minutes. A period boundary is a date, not an instant, and sweeping harder
# would be querying constantly to learn nothing.
POLL_SECONDS: Final = 600.0

# How many subscriptions one sweep advances. Bounded for the same reason the
# follow-up sweep is: the rows are held until the commit, and a deployment with
# ten thousand renewals on the first of the month should take several passes
# rather than one enormous transaction.
CLAIM_LIMIT: Final = 200


class BillingWorker:
    """Polls for subscriptions whose period has ended and advances them."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        claim_limit: int = CLAIM_LIMIT,
    ) -> None:
        self._database = database
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._claim_limit = claim_limit
        self._running = False
        # Set by stop(), so shutdown does not wait out a ten-minute interval.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("billing.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed sweep must not kill the loop: every later renewal
                # would go unprocessed and nothing would say why.
                logger.exception("billing.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("billing.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Advance every subscription whose period has ended.

        Returns how many were advanced. One session for the sweep, committed at
        the end, so a subscription that fails to advance leaves the rest intact.
        """
        moment = now or datetime.now(UTC)
        handled = 0

        async with self._database.session() as session:
            subscriptions = PlatformSubscriptionRepository(session)
            plans = PlanRepository(session)
            due = await subscriptions.due(now=moment, limit=self._claim_limit)
            if not due:
                return 0

            for subscription in due:
                plan = await plans.get_by_id(subscription.plan_id)
                if plan is None:
                    # RESTRICT on the foreign key makes this unreachable, and it
                    # is logged rather than crashed on: one impossible row must
                    # not strand every other workspace's renewal behind it.
                    logger.warning(
                        "billing.plan_missing_for_subscription",
                        extra={"subscription_id": str(subscription.id)},
                    )
                    continue

                previous = subscription.status
                await roll_over(subscription, plan=plan, now=moment)
                handled += 1
                logger.info(
                    "billing.subscription_advanced",
                    extra={
                        "event": "billing.subscription_advanced",
                        "tenant_id": str(subscription.tenant_id),
                        "from_status": previous.value,
                        "status": subscription.status.value,
                    },
                )

        logger.info("billing.sweep_completed", extra={"handled": handled})
        return handled


__all__ = ["CLAIM_LIMIT", "POLL_SECONDS", "BillingWorker"]
