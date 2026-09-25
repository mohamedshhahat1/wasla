"""The life of one workspace's subscription.

Everything here is a state change a person asked for, or one that time forced.
There are only five, and keeping them named rather than expressible as an
arbitrary status update is the point: `PATCH {"status": "active"}` is a route
that lets a customer end their own trial and start a free forever, and no amount
of validation afterwards makes that a good API.

- **start** — a workspace gets its first subscription. Starter is a free plan
  and starts `ACTIVE`; it never expires because a timer ran out (BILL-01).
- **change_plan** — a move the *platform* makes at once: a purchase being
  granted, a reversal withdrawing one, a free-to-free move.
- **request_plan** — what a customer asks for. A cheaper plan while a paid
  period is running is *scheduled* for the end of that period, so nothing
  already paid for is forfeited; a pricier one is a checkout (402 here).
- **apply_purchase** — a settled purchase: the paid version, a new period that
  starts at settlement, and a new billing anchor (BILL-03).
- **cancel** — at the end of the period the customer has paid for, or at once
  if they insist.
- **resume** — undo a cancellation that has not taken effect yet.
- **roll_over** — what the sweep does when a period ends: take a cancellation,
  apply a scheduled change, open the next period from the anchor.

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
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import (
    BillingInterval,
    Plan,
    PlanVersion,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.user import User
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.tenant_repository import TenantRepository
from app.services import billing_calendar
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate
from app.services.plan_catalog import PlanCatalog

logger = get_logger(__name__)


def add_interval(start: datetime, interval: BillingInterval) -> datetime:
    """The end of a period that began at `start`, `start` being its own anchor.

    See `app.services.billing_calendar`, which owns period arithmetic; kept here
    under its old name for the callers that import it from this module.
    """
    return billing_calendar.add_interval(start, interval)


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
        self._catalog = PlanCatalog(session)
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

    async def version_for(self, subscription: Subscription) -> PlanVersion | None:
        """The immutable terms this subscription is held to."""
        return await self._catalog.pinned_version(subscription)

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
        version = await self._require_version(plan, at=moment)
        # A trial of a *free* plan grants nothing extra and ended in `EXPIRED`
        # on day fourteen, which is where BILL-01 began: every workspace older
        # than two weeks paid for plans it was then refused. A free plan
        # therefore never trials, whatever its row says.
        trialing = version.trial_days > 0 and version.price > 0
        period_end = (
            moment + timedelta(days=version.trial_days)
            if trialing
            else billing_calendar.add_interval(moment, version.interval)
        )
        subscription = self._subscriptions.create(
            plan_id=plan.id,
            status=SubscriptionStatus.TRIALING if trialing else SubscriptionStatus.ACTIVE,
            current_period_start=moment,
            current_period_end=period_end,
            trial_ends_at=period_end if trialing else None,
        )
        subscription.plan_version_id = version.id
        subscription.billing_anchor_at = moment
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

        version = await self._require_version(plan, at=moment)
        previous = subscription.plan_id
        subscription.plan_id = plan.id
        subscription.plan_version_id = version.id
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.current_period_start = moment
        subscription.current_period_end = billing_calendar.add_interval(moment, version.interval)
        subscription.billing_anchor_at = moment
        # A trial does not survive a deliberate choice of plan: the customer has
        # decided, which is what the trial was for.
        subscription.trial_ends_at = None
        subscription.cancel_at_period_end = False
        subscription.cancelled_at = None
        subscription.clear_scheduled_change()

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

    async def request_plan(
        self,
        *,
        plan_code: str,
        now: datetime | None = None,
        actor: User | None = None,
    ) -> Subscription:
        """What a workspace owner asking for another plan gets (spec: downgrades).

        - **A free move from a free plan** takes effect at once, as it always did.
        - **A cheaper plan while a paid period runs** is *scheduled* for the end
          of that period. The customer keeps what they paid for; the change and
          the lower renewal price arrive together at the boundary. Moving them
          down at once used to forfeit the rest of a period they had bought.
        - **A pricier plan** is a purchase, and a purchase is a checkout: 402.

        Resources above the new plan's limits are never deleted by any of
        these. They stay; creating more is refused until usage fits again.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active or not plan.is_public:
            raise ValidationError("No such plan.")
        if subscription.plan_id == plan.id:
            raise ConflictError("This workspace is already on that plan.")
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))

        target = await self._require_version(plan, at=moment)
        current = await self._catalog.pinned_version(subscription, at=moment)
        current_price = current.price if current is not None else target.price

        if target.price > 0 and target.price >= current_price:
            raise PaymentRequiredError(
                f"The {plan.name} plan is not free. Start a checkout for it and "
                "the plan applies once the payment is confirmed."
            )
        if current_price <= 0:
            # Free to free: nothing is paid for, so nothing is forfeited.
            return await self.change_plan(plan_code=plan.code, now=moment, actor=actor)
        return await self.schedule_change(
            version=target,
            source=ScheduledChangeSource.DOWNGRADE,
            reason="Downgrade requested by the workspace owner.",
            now=moment,
            actor=actor,
        )

    async def schedule_change(
        self,
        *,
        version: PlanVersion,
        source: ScheduledChangeSource,
        reason: str,
        now: datetime | None = None,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
    ) -> Subscription:
        """Arrange for the subscription to move to `version` when its period ends.

        One pending change at a time: scheduling again replaces the previous
        one, and the trail records both. Applied once, by the billing sweep, at
        `current_period_end` - never here.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))
        if subscription.plan_version_id == version.id:
            raise ConflictError("This subscription is already on that version.")

        before = str(subscription.scheduled_plan_version_id or "")
        subscription.scheduled_plan_version_id = version.id
        subscription.scheduled_change_source = source
        subscription.scheduled_change_reason = reason
        subscription.scheduled_change_actor_id = actor.id if actor is not None else None
        subscription.scheduled_change_at = moment
        self._audit.record(
            AuditAction.SUBSCRIPTION_PLAN_CHANGE_SCHEDULED,
            actor=actor,
            actor_kind=actor_kind,
            target_type="subscription",
            target_id=subscription.id,
            meta={
                "source": source.value,
                "to_version_id": str(version.id),
                "effective_at": subscription.current_period_end.isoformat(),
                "replaced_version_id": before or None,
                "reason": reason,
            },
        )
        logger.info(
            "billing.plan_change_scheduled",
            extra={
                "event": "billing.plan_change_scheduled",
                "tenant_id": str(self._tenant_id),
                "source": source.value,
            },
        )
        return subscription

    async def cancel_scheduled_change(
        self,
        *,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
    ) -> Subscription:
        """Withdraw a plan change that has not taken effect yet."""
        subscription = await self._require_subscription()
        if not subscription.has_scheduled_change:
            raise ConflictError("No plan change is scheduled.")
        withdrawn = str(subscription.scheduled_plan_version_id)
        subscription.clear_scheduled_change()
        self._audit.record(
            AuditAction.SUBSCRIPTION_SCHEDULED_CHANGE_CANCELLED,
            actor=actor,
            actor_kind=actor_kind,
            target_type="subscription",
            target_id=subscription.id,
            meta={"version_id": withdrawn},
        )
        return subscription

    async def apply_purchase(
        self,
        *,
        version: PlanVersion,
        now: datetime,
        keep_cancellation: bool = False,
    ) -> tuple[Subscription, SubscriptionStatus | None]:
        """Put the workspace on the version a settled payment bought (BILL-01, BILL-03).

        The single place a paid purchase changes a subscription. The paid
        period starts **now** - at settlement - and ends one interval later,
        and the billing anchor moves to now with it. That is the whole of the
        advance-billing contract: the invoice that was just paid covers exactly
        `[now, anchor + 1 interval)`, and the first renewal the sweep raises is
        for the period *after* it, so no period is ever billed twice.

        A workspace with no subscription gets one, and a terminal one is
        brought back: the caller has already decided that this purchase is one
        the customer chose to make after it ended. Returns the subscription and
        the status it had before, or None when it was created here.

        `keep_cancellation` is for a customer who asked to cancel *after*
        opening the payment page they then paid: they receive the period they
        paid for and it does not renew.
        """
        subscription = await self._subscriptions.get()
        previous: SubscriptionStatus | None = None
        period_end = billing_calendar.add_interval(now, version.interval)
        if subscription is None:
            subscription = self._subscriptions.create(
                plan_id=version.plan_id,
                status=SubscriptionStatus.ACTIVE,
                current_period_start=now,
                current_period_end=period_end,
            )
        else:
            previous = subscription.status
            subscription.plan_id = version.plan_id
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.current_period_start = now
            subscription.current_period_end = period_end
            subscription.trial_ends_at = None
            subscription.ended_at = None
            if not keep_cancellation:
                subscription.cancel_at_period_end = False
                subscription.cancelled_at = None
        subscription.plan_version_id = version.id
        subscription.billing_anchor_at = now
        subscription.clear_scheduled_change()
        await self._session.flush()

        reactivated = previous is not None and previous in (
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
        )
        self._audit.record(
            (
                AuditAction.SUBSCRIPTION_REACTIVATED
                if reactivated
                else (
                    AuditAction.SUBSCRIPTION_STARTED
                    if previous is None
                    else AuditAction.SUBSCRIPTION_PLAN_CHANGED
                )
            ),
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="subscription",
            target_id=subscription.id,
            meta={
                "plan_version_id": str(version.id),
                "version": version.version,
                "period_start": now.isoformat(),
                "period_end": period_end.isoformat(),
                "from_status": previous.value if previous is not None else None,
                "reason": "purchase_settled",
            },
        )
        return subscription, previous

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

    async def _require_version(self, plan: Plan, *, at: datetime) -> PlanVersion:
        """The version a new holder of `plan` gets at `at`."""
        version = await self._catalog.current_version(plan, at=at)
        if version is None:
            raise ValidationError("No such plan.")
        return version

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
        current = await self._catalog.current_version(plan) if self_service else None
        if self_service and current is not None and current.price > 0:
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


async def bootstrap_default_subscription(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Put a newly created workspace on the plan an operator configured.

    Shared by the two paths that create a workspace - registration, and
    `POST /workspaces` - because they must not be able to disagree about it. It
    was `AuthService._start_subscription`, reachable only from registration; a
    second copy in the workspace service would be a second policy, and a policy
    that exists twice is one that differs the first time either is touched.

    **Creating a workspace must not fail because a catalogue row is missing.**
    A workspace without a subscription is still entitled to the default plan by
    the same code (ADR-029), so the worst case here is an absent row rather than
    a customer who cannot get started - and a signup that 500s over billing
    configuration is the least forgivable failure in the product.

    `self_service=False`: this is the platform putting a new workspace on the
    configured default, not a customer choosing from the catalogue. A deployment
    whose default plan is private is making a deliberate choice, and workspace
    creation should not start failing because of it.
    """
    code = settings.default_plan_code
    if not code:
        return
    try:
        await SubscriptionService(session, tenant_id=tenant_id, settings=settings).start(
            plan_code=code,
            self_service=False,
        )
    except ValidationError:
        logger.warning(
            "billing.default_plan_missing",
            extra={
                "event": "billing.default_plan_missing",
                "tenant_id": str(tenant_id),
                "plan_code": code,
            },
        )


def legacy_anchor(subscription: Subscription, interval: BillingInterval) -> datetime:
    """The anchor of a subscription written before anchors were stored.

    Its period start, when the period is exactly one interval from it - which
    is every period this application ever opened, so the original day survives
    (31 January stays the 31st). Otherwise its period end: a row somebody wrote
    by hand with an irregular period keeps renewing one interval at a time from
    where it actually is, rather than being snapped to a short first period.
    """
    start = subscription.current_period_start
    if billing_calendar.add_interval(start, interval) == subscription.current_period_end:
        return start
    return subscription.current_period_end


async def roll_over(
    subscription: Subscription,
    *,
    plan: Plan | PlanVersion,
    now: datetime | None = None,
    next_version: PlanVersion | None = None,
) -> Subscription:
    """Advance a subscription whose period has ended.

    Pure state, no I/O, so the rules are testable without a database and the
    sweep that calls this is left with nothing but the query and the commit.

    `plan` is the terms of the period that is ending; `next_version` the terms
    of the one that opens, when they differ - a scheduled downgrade the sweep
    has already decided to apply. Four outcomes, decided entirely by the row:

    - A cancellation was pending: it takes effect now.
    - A trial of a *priced* plan ended: `EXPIRED`, because nobody decided it. A
      free plan's "trial" is not a trial at all (BILL-01) - it simply rolls on.
    - A scheduled change applies: the next period opens on the new version.
    - Otherwise the next period opens on the same one. The subscription stays
      whatever it was - including `PAST_DUE`, since a new period does not
      settle an old debt.

    The new period ends at the next anniversary of the billing anchor, not one
    interval after the old end (BILL-18). An interval change - monthly to
    yearly - re-anchors at the boundary, because an anchor only means anything
    against one interval.
    """
    moment = now if now is not None else datetime.now(UTC)

    if subscription.cancel_at_period_end:
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.ended_at = moment
        subscription.clear_scheduled_change()
        return subscription

    if subscription.status is SubscriptionStatus.TRIALING:
        if plan.price > 0:
            subscription.status = SubscriptionStatus.EXPIRED
            subscription.ended_at = moment
            return subscription
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.trial_ends_at = None

    terms = next_version if next_version is not None else plan
    start = subscription.current_period_end
    anchor = subscription.billing_anchor_at or legacy_anchor(subscription, plan.interval)
    if next_version is not None:
        subscription.plan_id = next_version.plan_id
        subscription.plan_version_id = next_version.id
        if next_version.interval is not plan.interval:
            anchor = start
    subscription.billing_anchor_at = anchor
    subscription.current_period_start = start
    subscription.current_period_end = billing_calendar.next_boundary(anchor, start, terms.interval)
    return subscription
