"""The life of one workspace's subscription.

Everything here is a state change a person asked for, or one that time forced.
There are only five, and keeping them named rather than expressible as an
arbitrary status update is the point: `PATCH {"status": "active"}` is a route
that lets a customer end their own trial and start a free forever, and no amount
of validation afterwards makes that a good API.

- **start** — a workspace gets its first subscription, on trial if the plan
  offers one.
- **change_plan** — an upgrade or a downgrade, effective now.
- **cancel** — at the end of the period the customer has paid for, or at once
  if they insist.
- **resume** — undo a cancellation that has not taken effect yet.
- **roll_over** — what the sweep does when a period ends: end a trial, or open
  the next period.

Payment is deliberately absent. A subscription is a complete, usable record
without a provider, which is what lets the whole of this work in local
development and in tests; when a provider arrives it fills in `provider` and
`provider_reference` and moves `past_due` around, and none of the rules here
change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository

logger = get_logger(__name__)


def add_interval(start: datetime, interval: BillingInterval) -> datetime:
    """The end of a period that began at `start`.

    Calendar arithmetic rather than a fixed number of days. Nobody bills in
    30-day units: "monthly" means the same date next month, and 30 days drifts a
    renewal backwards through the year until it lands in the wrong month. A
    workspace that started on the 31st renews on the 30th in November and the
    28th in February, which is what every subscription product does and what a
    customer expects to see.
    """
    if interval is BillingInterval.YEARLY:
        return _same_day(start, year=start.year + 1, month=start.month)
    if start.month == 12:
        return _same_day(start, year=start.year + 1, month=1)
    return _same_day(start, year=start.year, month=start.month + 1)


def _same_day(moment: datetime, *, year: int, month: int) -> datetime:
    """`moment` in another month, clamped to that month's last day."""
    day = moment.day
    while day > 0:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            # The 31st of a 30-day month, or the 29th of a common February.
            day -= 1
    raise ValueError("No valid day in the target month.")  # pragma: no cover


class SubscriptionService:
    """Subscription operations for one workspace."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)

    async def get(self) -> Subscription | None:
        return await self._subscriptions.get()

    async def plan_for(self, subscription: Subscription) -> Plan:
        plan = await self._plans.get_by_id(subscription.plan_id)
        if plan is None:  # pragma: no cover - RESTRICT makes this unreachable
            raise NotFoundError("This subscription's plan no longer exists.")
        return plan

    async def start(
        self,
        *,
        plan_code: str,
        now: datetime | None = None,
    ) -> Subscription:
        """Give a workspace its first subscription.

        Trials are the plan's decision, not the caller's: a caller that could
        ask for a trial length is a caller that can ask for a thousand days.
        """
        moment = now if now is not None else datetime.now(UTC)
        if await self._subscriptions.get() is not None:
            raise ConflictError("This workspace already has a subscription.")

        plan = await self._require_plan(plan_code)
        trialing = plan.trial_days > 0
        period_end = (
            moment + timedelta(days=plan.trial_days)
            if trialing
            else add_interval(moment, plan.interval)
        )
        subscription = self._subscriptions.create(
            plan_id=plan.id,
            status=SubscriptionStatus.TRIALING if trialing else SubscriptionStatus.ACTIVE,
            current_period_start=moment,
            current_period_end=period_end,
            trial_ends_at=period_end if trialing else None,
        )
        # Flushed so the caller can read the row it just created - primary keys
        # and server defaults are not populated until the insert reaches the
        # database, and a route that returns this would otherwise answer 500.
        await self._session.flush()
        logger.info(
            "billing.subscription_started",
            extra={
                "event": "billing.subscription_started",
                "tenant_id": str(self._tenant_id),
                "plan": plan.code,
                "trialing": trialing,
            },
        )
        return subscription

    async def change_plan(self, *, plan_code: str, now: datetime | None = None) -> Subscription:
        """Move to another plan, effective immediately.

        The period restarts, and that cuts both ways on purpose: an upgrade
        takes effect at once, and so does the new period's usage allowance. No
        proration is attempted - money is not moved by this system yet, and
        inventing a credit that no invoice reflects would be worse than not
        having one.

        A cancellation pending on the old plan is cleared. Somebody choosing a
        new plan has plainly changed their mind about leaving.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        plan = await self._require_plan(plan_code)

        if subscription.plan_id == plan.id:
            raise ConflictError("This workspace is already on that plan.")
        if subscription.is_terminal:
            raise ConflictError("This subscription has ended. Start a new one instead.")

        previous = subscription.plan_id
        subscription.plan_id = plan.id
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.current_period_start = moment
        subscription.current_period_end = add_interval(moment, plan.interval)
        # A trial does not survive a deliberate choice of plan: the customer has
        # decided, which is what the trial was for.
        subscription.trial_ends_at = None
        subscription.cancel_at_period_end = False
        subscription.cancelled_at = None

        logger.info(
            "billing.plan_changed",
            extra={
                "event": "billing.plan_changed",
                "tenant_id": str(self._tenant_id),
                "from_plan_id": str(previous),
                "plan": plan.code,
            },
        )
        return subscription

    async def cancel(
        self, *, immediately: bool = False, now: datetime | None = None
    ) -> Subscription:
        """Stop the subscription, at the end of the period or at once.

        The default is at the end. A customer who has paid for a month keeps the
        month; ending it the instant they click is taking something they bought,
        and it is also the behaviour that makes people afraid to click.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError("This subscription has already ended.")

        subscription.cancelled_at = moment
        if immediately:
            subscription.status = SubscriptionStatus.CANCELLED
            subscription.ended_at = moment
            subscription.cancel_at_period_end = False
            # The period ends now, so nothing counts against an allowance the
            # workspace no longer has.
            subscription.current_period_end = moment
        else:
            subscription.cancel_at_period_end = True

        logger.info(
            "billing.subscription_cancelled",
            extra={
                "event": "billing.subscription_cancelled",
                "tenant_id": str(self._tenant_id),
                "immediately": immediately,
            },
        )
        return subscription

    async def resume(self) -> Subscription:
        """Undo a cancellation that has not taken effect yet."""
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError("This subscription has ended. Start a new one instead.")
        if not subscription.cancel_at_period_end:
            raise ConflictError("This subscription is not scheduled to end.")

        subscription.cancel_at_period_end = False
        subscription.cancelled_at = None
        logger.info(
            "billing.subscription_resumed",
            extra={
                "event": "billing.subscription_resumed",
                "tenant_id": str(self._tenant_id),
            },
        )
        return subscription

    async def _require_subscription(self) -> Subscription:
        subscription = await self._subscriptions.get()
        if subscription is None:
            raise NotFoundError("This workspace has no subscription.")
        return subscription

    async def _require_plan(self, plan_code: str) -> Plan:
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active:
            # A retired plan is invisible to a chooser even though existing
            # subscriptions still point at it.
            raise ValidationError("No such plan.")
        return plan


async def roll_over(
    subscription: Subscription,
    *,
    plan: Plan,
    now: datetime | None = None,
) -> Subscription:
    """Advance a subscription whose period has ended.

    Pure state, no I/O, so the rules are testable without a database and the
    sweep that calls this is left with nothing but the query and the commit.

    Three outcomes, and which one applies is decided entirely by the row:

    - A cancellation was pending: it takes effect now.
    - A trial ended and nobody chose a plan: `EXPIRED`, not `CANCELLED`, because
      nobody decided it.
    - Otherwise the next period opens. The subscription stays whatever it was -
      including `PAST_DUE`, since a new period does not settle an old debt.
    """
    moment = now if now is not None else datetime.now(UTC)

    if subscription.cancel_at_period_end:
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.ended_at = moment
        return subscription

    if subscription.status is SubscriptionStatus.TRIALING:
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.ended_at = moment
        return subscription

    subscription.current_period_start = subscription.current_period_end
    subscription.current_period_end = add_interval(subscription.current_period_start, plan.interval)
    return subscription
