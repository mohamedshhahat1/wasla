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

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.models.audit import AuditAction
from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.user import User
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.tenant_repository import TenantRepository
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate

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


def _unusable_reason(subscription: Subscription) -> str:
    """Why a terminal subscription cannot be changed, in the customer's terms.

    Three call sites refuse `is_terminal` and all three used to say "this
    subscription has ended", which stopped being true when `SUSPENDED` joined
    the set (ADR-061). A suspended workspace has not ended anything - it owes
    money - and telling it to start a new subscription would send somebody down
    a path that does not fix their problem.

    The refusal itself is unchanged and correct in both cases: you cannot
    cancel, resume or downgrade your way out of an unpaid invoice.
    """
    if subscription.is_suspended_for_non_payment:
        return "This workspace is suspended for an unpaid invoice. " "Settle it to restore service."
    return "This subscription has ended. Start a new one instead."


class SubscriptionService:
    """Subscription operations for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._tenants = TenantRepository(session)
        # Every operation here changes what the workspace pays, which is the
        # definition of an action somebody is asked about later.
        self._audit = AuditTrail(session, tenant_id=tenant_id)
        # Defaulted rather than required so existing construction sites keep
        # working; the request-scoped provider passes the real settings in.
        self._outbox = EmailOutbox(session, settings if settings is not None else get_settings())

    async def _workspace_name(self) -> str:
        """The tenant's own name, for a template that mentions it.

        Read from the row rather than taken from a caller: it is the only
        variable any billing template carries, and a workspace name that came
        from a request would be a tenant-controlled string in somebody's
        inbox.
        """
        tenant = await self._tenants.get_by_id(self._tenant_id)
        return tenant.name if tenant is not None else "your workspace"

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
        actor: User | None = None,
        self_service: bool = True,
    ) -> Subscription:
        """Give a workspace its first subscription.

        Trials are the plan's decision, not the caller's: a caller that could
        ask for a trial length is a caller that can ask for a thousand days.
        """
        moment = now if now is not None else datetime.now(UTC)
        if await self._subscriptions.get() is not None:
            raise ConflictError("This workspace already has a subscription.")

        plan = await self._require_plan(plan_code, self_service=self_service)
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
        self._audit.record(
            AuditAction.SUBSCRIPTION_STARTED,
            actor=actor,
            target_type="subscription",
            target_id=subscription.id,
            target_label=plan.code,
            meta={"trialing": trialing},
        )
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

    async def change_plan(
        self,
        *,
        plan_code: str,
        now: datetime | None = None,
        actor: User | None = None,
        self_service: bool = True,
    ) -> Subscription:
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
        plan = await self._require_plan(plan_code, self_service=self_service)

        if subscription.plan_id == plan.id:
            raise ConflictError("This workspace is already on that plan.")
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))

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

        self._audit.record(
            AuditAction.SUBSCRIPTION_PLAN_CHANGED,
            actor=actor,
            target_type="subscription",
            target_id=subscription.id,
            target_label=plan.code,
            meta={"from_plan_id": str(previous)},
        )
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
        self,
        *,
        immediately: bool = False,
        now: datetime | None = None,
        actor: User | None = None,
    ) -> Subscription:
        """Stop the subscription, at the end of the period or at once.

        The default is at the end. A customer who has paid for a month keeps the
        month; ending it the instant they click is taking something they bought,
        and it is also the behaviour that makes people afraid to click.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))

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

        self._audit.record(
            AuditAction.SUBSCRIPTION_CANCELLED,
            actor=actor,
            target_type="subscription",
            target_id=subscription.id,
            meta={"immediately": immediately},
        )
        # Queued on this session, so the notice and the cancellation commit
        # together (ADR-042). Keyed on the moment it happened, so cancelling
        # again after a resume notifies again while a retried request does not.
        await self._outbox.enqueue_for_tenant_owners(
            tenant_id=self._tenant_id,
            template=EmailTemplate.SUBSCRIPTION_CANCELLED,
            idempotency_prefix=f"subscription-cancelled:{subscription.id}:{moment.isoformat()}",
            context={"workspace_name": await self._workspace_name()},
        )
        logger.info(
            "billing.subscription_cancelled",
            extra={
                "event": "billing.subscription_cancelled",
                "tenant_id": str(self._tenant_id),
                "immediately": immediately,
            },
        )
        return subscription

    async def resume(self, *, actor: User | None = None) -> Subscription:
        """Undo a cancellation that has not taken effect yet."""
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))
        if not subscription.cancel_at_period_end:
            raise ConflictError("This subscription is not scheduled to end.")

        subscription.cancel_at_period_end = False
        subscription.cancelled_at = None
        self._audit.record(
            AuditAction.SUBSCRIPTION_RESUMED,
            actor=actor,
            target_type="subscription",
            target_id=subscription.id,
        )
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

    async def _require_plan(self, plan_code: str, *, self_service: bool = True) -> Plan:
        """The plan a caller named, if they are allowed to name it.

        `is_active` was always checked: a retired plan is invisible to a chooser
        even though existing subscriptions still point at it.

        `is_public` was **not**, and that was a hole rather than an oversight in
        naming. `GET /billing/plans` filters the catalogue down to public plans,
        so a private one - Enterprise, or anything negotiated for one customer -
        never appears in the list. Nothing stopped a workspace owner from
        posting its code anyway, and `start` and `change_plan` would move them
        onto it with its limits and its price. The catalogue was a display
        filter standing in for an authorization rule.

        `self_service=False` is how a plan that is not on offer is still
        assignable by something inside the platform - the registration path
        putting a new workspace on the configured default, and any future
        operator action. It has to be passed explicitly, so the permissive path
        is never the one a caller gets by forgetting.
        """
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active:
            raise ValidationError("No such plan.")
        if self_service and not plan.is_public:
            # Deliberately the same refusal as a plan that does not exist. A
            # distinct message would confirm that a private plan code is real,
            # which is exactly what somebody guessing codes wants to learn.
            raise ValidationError("No such plan.")
        if self_service and plan.price > 0:
            # The commercial invariant, enforced in the one place both doors
            # pass through (ADR-059).
            #
            # `start` and `change_plan` used to grant any public plan outright,
            # so a workspace owner could post `{"plan_code": "business"}` and
            # hold every Business limit without a payment existing anywhere.
            # The money pipeline beside it was already strict - invoice,
            # payment, signed callback, amount and currency checked, legal
            # transition - and simply had nothing to do with which plan a
            # workspace was on. This is the join between them.
            #
            # A **priced** plan is now reached only through `POST
            # /billing/checkout`, and applied only by `CheckoutService._settle`
            # when a verified callback says the invoice is paid. A **free**
            # plan is unaffected: downgrading to the default plan, and every
            # deployment whose catalogue is free, works exactly as before.
            #
            # `self_service=False` is what settlement and registration pass, so
            # the platform can still assign what a customer may not ask for.
            # It has to be passed explicitly, which is what keeps the
            # permissive path from being the one a caller gets by forgetting.
            raise PaymentRequiredError(
                f"The {plan.name} plan is not free. Start a checkout for it and "
                "the plan applies once the payment is confirmed."
            )
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
