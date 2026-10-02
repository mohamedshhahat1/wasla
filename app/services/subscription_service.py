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
- **request_plan** — what a customer asks for. A cheaper plan, or a shorter
  billing term, while a paid term is running is *scheduled* for the end of
  that term, so nothing already paid for is forfeited; a pricier one, or a
  longer term, is a checkout (402 here). See `commercial_policy`.
- **apply_purchase** — a settled purchase: the paid version at the paid price,
  a new billing term that starts at settlement, its first monthly usage cycle,
  and a new billing anchor (BILL-03, ADR-116).
- **cancel** — at the end of the period the customer has paid for, or at once
  if they insist.
- **resume** — undo a cancellation that has not taken effect yet.
- **roll_over** — what the sweep does when a term ends: take a cancellation,
  apply a scheduled change, open the next term from the anchor and its first
  usage cycle.

**Two periods, never conflated** (ADR-116). `current_period_*` is the billing
term a price paid for - a month or a year. `usage_period_*` is the calendar
month the usage allowances count over. On a monthly price they are the same
window; on a yearly price the term holds twelve cycles, and the sweep advances
the cycle monthly without billing anything.

Payment is deliberately absent. A subscription is a complete, usable record
without a provider, which is what lets the whole of this work in local
development and in tests; when a provider arrives it fills in `provider` and
`provider_reference` and moves `past_due` around, and none of the rules here
change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    PlanPrice,
    PlanVersion,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.channel_capacity import CapacityReductionCause
from app.db.models.user import User
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.tenant_repository import TenantRepository
from app.services import billing_calendar
from app.services.audit_service import AuditTrail
from app.services.capacity_reduction import ChannelCapacityReductions
from app.services.commercial_policy import ChangeTiming, Terms, change_timing
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate
from app.services.plan_catalog import NO_SUCH_PRICE, PlanCatalog

logger = get_logger(__name__)

# The audit reason a settled purchase records. Only a purchase: a period
# granted with no payment names its own basis (`PlanGrant`).
PURCHASE_SETTLED = "purchase_settled"
# A free version put on a workspace by a platform operator.
PLATFORM_GRANT = "platform_grant"
# A priced version granted without payment, backed by a billing adjustment.
COMPLIMENTARY_GRANT = "complimentary_grant"


@dataclass(frozen=True, slots=True)
class PlanGrant:
    """Who put a workspace on a version without a purchase, and on what basis.

    `basis` is `PLATFORM_GRANT` or `COMPLIMENTARY_GRANT`; `reason` is what the
    operator wrote. Carried into the subscription's own audit row so the trail
    says who granted what and that nothing was paid (PAY-E2E-02).
    """

    actor: User
    basis: str
    reason: str | None = None


def add_interval(start: datetime, interval: BillingInterval) -> datetime:
    """The end of a period that began at `start`, `start` being its own anchor.

    See `app.services.billing_calendar`, which owns period arithmetic; kept here
    under its old name for the callers that import it from this module.
    """
    return billing_calendar.add_interval(start, interval)


def term_end(start: datetime, *, version: PlanVersion, price: PlanPrice | None) -> datetime:
    """Where a billing term that begins at `start` ends (ADR-116).

    The price decides: one month on a monthly price, twelve calendar months on
    a yearly one. A free version has no price and bills nothing; its term is
    the calendar month its version was published with, as it always was.
    """
    if price is not None:
        return billing_calendar.add_interval(start, price.billing_interval, price.interval_count)
    return billing_calendar.add_interval(start, version.interval)


def open_term(subscription: Subscription, *, start: datetime, end: datetime) -> None:
    """Make `[start, end)` the billing term and open its first usage cycle.

    The one place both periods move together, so no path can start a term
    and leave the previous term's usage cycle behind it.
    """
    subscription.current_period_start = start
    subscription.current_period_end = end
    subscription.usage_period_start, subscription.usage_period_end = (
        billing_calendar.first_usage_period(subscription)
    )


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

    async def price_for(
        self, subscription: Subscription, version: PlanVersion | None
    ) -> PlanPrice | None:
        """The price this subscription renews at; None on a free version."""
        if version is None:
            return None
        return await self._catalog.pinned_price(subscription, version=version)

    async def scheduled_price_for(self, subscription: Subscription) -> PlanPrice | None:
        """The exact price a scheduled change will renew at, if one is scheduled."""
        return await self._catalog.get_price(subscription.scheduled_plan_price_id)

    async def prices_for(self, version: PlanVersion | None) -> list[PlanPrice]:
        """The price options a version is currently sold at."""
        return await self._catalog.prices(version) if version is not None else []

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
        # Only the platform starts a workspace on a priced plan here (a
        # customer is sent to a checkout by `_require_plan`), and it does so
        # at the plan's default price - the monthly one where there is one.
        price = None if version.is_free else await self._catalog.default_price(version)
        period_end = (
            moment + timedelta(days=version.trial_days)
            if trialing
            else term_end(moment, version=version, price=price)
        )
        subscription = self._subscriptions.create(
            plan_id=plan.id,
            status=SubscriptionStatus.TRIALING if trialing else SubscriptionStatus.ACTIVE,
            current_period_start=moment,
            current_period_end=period_end,
            trial_ends_at=period_end if trialing else None,
        )
        subscription.plan_version_id = version.id
        subscription.plan_price_id = price.id if price is not None else None
        subscription.billing_anchor_at = moment
        open_term(subscription, start=moment, end=period_end)
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
        price = None if version.is_free else await self._catalog.default_price(version)
        previous = subscription.plan_id
        subscription.plan_id = plan.id
        subscription.plan_version_id = version.id
        subscription.plan_price_id = price.id if price is not None else None
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.billing_anchor_at = moment
        open_term(subscription, start=moment, end=term_end(moment, version=version, price=price))
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
        plan_code: str | None = None,
        plan_price_id: uuid.UUID | None = None,
        now: datetime | None = None,
        actor: User | None = None,
    ) -> Subscription:
        """What a workspace owner asking for another plan or term gets.

        The customer names a price (`plan_price_id`) - or, for compatibility,
        only a plan code, which means that plan's default (monthly) price.
        `commercial_policy.change_timing` then decides (ADR-112, ADR-116):

        - **Free to free** takes effect at once, as it always did.
        - **A lower tier, or a shorter term, while a paid term runs** is
          *scheduled* for the end of that term, pinned to the exact price
          chosen. The customer keeps what they paid for - a yearly customer
          moving to monthly keeps the rest of their year - and the change and
          its price arrive together at the boundary.
        - **A higher tier, or a longer term** is a purchase, and a purchase is
          a checkout: 402.

        Resources above the new plan's limits are never deleted by any of
        these. They stay; creating more is refused until usage fits again.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))
        plan, target, target_price = await self._requested_terms(
            plan_code=plan_code, plan_price_id=plan_price_id, at=moment
        )
        current = await self._catalog.pinned_version(subscription, at=moment)
        current_price = (
            await self._catalog.pinned_price(subscription, version=current)
            if current is not None
            else None
        )
        timing = await self.timing(
            current=Terms(current, current_price) if current is not None else None,
            target=Terms(target, target_price),
        )
        if timing is ChangeTiming.UNCHANGED:
            raise ConflictError("This workspace is already on that plan and billing term.")
        if timing is ChangeTiming.PURCHASE_NOW:
            raise PaymentRequiredError(
                f"The {plan.name} plan at that price is a purchase. Start a checkout for "
                "it and it applies once the payment is confirmed."
            )
        if timing is ChangeTiming.FREE_NOW:
            # Free to free: nothing is paid for, so nothing is forfeited.
            return await self.change_plan(plan_code=plan.code, now=moment, actor=actor)
        return await self.schedule_change(
            version=target,
            price=target_price,
            source=ScheduledChangeSource.DOWNGRADE,
            reason="Change requested by the workspace owner for the end of the paid term.",
            now=moment,
            actor=actor,
        )

    async def timing(self, *, current: Terms | None, target: Terms) -> ChangeTiming:
        """`change_timing`, with the target priced on the current term if it can be."""
        like_for_like = None
        if current is not None and current.price is not None and target.price is not None:
            like_for_like = await self._catalog.price_for_term(
                target.version,
                interval=current.price.billing_interval,
                interval_count=current.price.interval_count,
            )
        return change_timing(current, target, target_on_current_term=like_for_like)

    async def _requested_terms(
        self,
        *,
        plan_code: str | None,
        plan_price_id: uuid.UUID | None,
        at: datetime,
    ) -> tuple[Plan, PlanVersion, PlanPrice | None]:
        """The plan, version and price a customer asked for, if they may have it."""
        if plan_price_id is not None:
            terms = await self._catalog.selectable_price(
                plan_price_id, tenant_id=self._tenant_id, at=at
            )
            if not self._on_offer(terms.plan):
                raise ValidationError(NO_SUCH_PRICE)
            if plan_code is not None and plan_code.strip().lower() != terms.plan.code:
                raise ValidationError("That price is not a price of that plan.")
            return terms.plan, terms.version, terms.price
        plan = await self._plans.get_by_code(plan_code or "")
        if plan is None or not self._on_offer(plan):
            raise ValidationError("No such plan.")
        version = await self._require_version(plan, at=at)
        if version.is_free:
            return plan, version, None
        price = await self._catalog.default_price(version)
        if price is None:
            raise ValidationError(
                "That plan is not sold monthly. Name the price you want with plan_price_id."
            )
        return plan, version, price

    async def schedule_change(
        self,
        *,
        version: PlanVersion,
        price: PlanPrice | None,
        source: ScheduledChangeSource,
        reason: str,
        now: datetime | None = None,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
    ) -> Subscription:
        """Arrange for the subscription to move to `version` at `price` when its term ends.

        The price is pinned now (ADR-116): a price published or retired before
        the boundary does not change what was agreed, and the renewal at the
        boundary bills exactly this one - a yearly term if it is yearly.

        One pending change at a time: scheduling again replaces the previous
        one, and the trail records both. Applied once, by the billing sweep, at
        `current_period_end` - never here.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._require_subscription()
        if subscription.is_terminal:
            raise ConflictError(_unusable_reason(subscription))
        if (price is None) != version.is_free or (
            price is not None and price.plan_version_id != version.id
        ):
            raise ValidationError("A scheduled change names a price of its own version.")
        if subscription.plan_version_id == version.id and (
            price is None or subscription.plan_price_id == price.id
        ):
            raise ConflictError("This subscription is already on that version and price.")
        await self._catalog.require_available(version, tenant_id=self._tenant_id)

        before = str(subscription.scheduled_plan_version_id or "")
        subscription.scheduled_plan_version_id = version.id
        subscription.scheduled_plan_price_id = price.id if price is not None else None
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
                "to_price_id": str(price.id) if price is not None else None,
                "billing_interval": price.billing_interval.value if price is not None else None,
                "interval_count": price.interval_count if price is not None else None,
                "amount": str(price.amount) if price is not None else "0.00",
                "currency": price.currency if price is not None else version.currency,
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
        price: PlanPrice | None,
        now: datetime,
        keep_cancellation: bool = False,
        grant: PlanGrant | None = None,
    ) -> tuple[Subscription, SubscriptionStatus | None]:
        """Put the workspace on the version and price a settled payment bought.

        The single place a paid purchase changes a subscription (BILL-01,
        BILL-03, ADR-116). The paid term starts **now** - at settlement - and
        ends one term of `price` later: a month, or twelve calendar months for
        a yearly price. The billing anchor moves to now with it, and the first
        monthly usage cycle opens now too. That is the whole of the
        advance-billing contract: the invoice that was just paid covers exactly
        `[now, anchor + 1 term)`, and the first renewal the sweep raises is for
        the term *after* it, so no term is ever billed twice. A yearly purchase
        changes nothing about usage allowances except that they now reset
        monthly inside a year that is already paid.

        `price` is None exactly when `version` is free.

        A workspace with no subscription gets one, and a terminal one is
        brought back: the caller has already decided that this purchase is one
        the customer chose to make after it ended. Returns the subscription and
        the status it had before, or None when it was created here.

        `keep_cancellation` is for a customer who asked to cancel *after*
        opening the payment page they then paid: they receive the period they
        paid for and it does not renew.

        `grant` is for the same period change made with **no** purchase - a
        platform operator assigning a free version, or a priced one as a
        complimentary grant. The trail then names that operator and the basis
        instead of claiming a settlement nobody paid for (PAY-E2E-02). Absent,
        the change is a settled purchase, recorded by the system.
        """
        await self._catalog.require_available(version, tenant_id=self._tenant_id)
        if (price is None) != version.is_free or (
            price is not None and price.plan_version_id != version.id
        ):
            raise ValidationError("A purchase grants a version at one of its own prices.")
        subscription = await self._subscriptions.get()
        previous: SubscriptionStatus | None = None
        period_end = term_end(now, version=version, price=price)
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
            subscription.trial_ends_at = None
            subscription.ended_at = None
            if not keep_cancellation:
                subscription.cancel_at_period_end = False
                subscription.cancelled_at = None
        subscription.plan_version_id = version.id
        subscription.plan_price_id = price.id if price is not None else None
        subscription.billing_anchor_at = now
        open_term(subscription, start=now, end=period_end)
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
            actor=grant.actor if grant is not None else None,
            actor_kind=(
                AuditActorKind.PLATFORM_STAFF if grant is not None else AuditActorKind.SYSTEM
            ),
            target_type="subscription",
            target_id=subscription.id,
            meta={
                "plan_version_id": str(version.id),
                "version": version.version,
                "plan_price_id": str(price.id) if price is not None else None,
                "billing_interval": price.billing_interval.value if price is not None else None,
                "amount": str(price.amount) if price is not None else "0.00",
                "period_start": now.isoformat(),
                "period_end": period_end.isoformat(),
                "usage_period_end": subscription.usage_period_end.isoformat(),
                "from_status": previous.value if previous is not None else None,
                "reason": grant.basis if grant is not None else PURCHASE_SETTLED,
                **({"operator_reason": grant.reason, "payment": None} if grant is not None else {}),
            },
        )
        # New terms from now are a capacity boundary (ENT-14): an upgrade that
        # makes everything fit closes an open reduction, a purchase of fewer
        # slots opens one, and an owner's pre-selection for the old term ends.
        await ChannelCapacityReductions(self._session, tenant_id=self._tenant_id).boundary(
            cause=CapacityReductionCause.DOWNGRADE, now=now
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
            subscription.cancel_at_period_end = False
            # The term and its usage cycle end now, so nothing counts against
            # an allowance the workspace no longer has.
            subscription.end_service_at(moment)
        else:
            # At the end of the paid *billing term* - a year on a yearly price,
            # not the end of the current monthly usage cycle (ADR-116). Usage
            # cycles keep rolling monthly until then.
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

    def _on_offer(self, plan: Plan) -> bool:
        """Whether this workspace's customer may choose `plan` themselves.

        Active, and either on the public catalogue or this workspace's own
        custom plan (ADR-113). Another workspace's custom plan is not on offer
        and is refused exactly like a code that does not exist.
        """
        if not plan.is_active:
            return False
        return plan.is_public or (plan.is_custom and plan.tenant_id == self._tenant_id)

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
        if self_service and not self._on_offer(plan):
            # Deliberately the same refusal as a plan that does not exist. A
            # distinct message would confirm that a private plan code is real,
            # which is exactly what somebody guessing codes wants to learn -
            # and that includes another workspace's custom plan (ADR-113).
            raise ValidationError("No such plan.")
        # The platform may assign what a customer may not choose, but never
        # another workspace's custom plan (ADR-113).
        await self._catalog.require_available(plan, tenant_id=self._tenant_id)
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


def legacy_anchor(
    subscription: Subscription, interval: BillingInterval, interval_count: int = 1
) -> datetime:
    """The anchor of a subscription written before anchors were stored.

    Its period start, when the period is exactly one term from it - which is
    every period this application ever opened, so the original day survives
    (31 January stays the 31st). Otherwise its period end: a row somebody wrote
    by hand with an irregular period keeps renewing one term at a time from
    where it actually is, rather than being snapped to a short first period.
    """
    start = subscription.current_period_start
    if (
        billing_calendar.add_interval(start, interval, interval_count)
        == subscription.current_period_end
    ):
        return start
    return subscription.current_period_end


def _term_shape(terms: Plan | PlanVersion, price: PlanPrice | None) -> tuple[BillingInterval, int]:
    """The billing term a price - or, for a free version, its version - renews on."""
    if price is not None:
        return price.billing_interval, price.interval_count
    return terms.interval, 1


async def roll_over(
    subscription: Subscription,
    *,
    plan: Plan | PlanVersion,
    price: PlanPrice | None = None,
    now: datetime | None = None,
    next_version: PlanVersion | None = None,
    next_price: PlanPrice | None = None,
    term_price: PlanPrice | None = None,
) -> Subscription:
    """Advance a subscription whose billing term has ended.

    Pure state, no I/O, so the rules are testable without a database and the
    sweep that calls this is left with nothing but the query and the commit.

    `plan` and `price` are the terms of the term that is ending; `next_version`
    and `next_price` the terms of the one that opens, when they differ - a
    scheduled change the sweep has already decided to apply. Four outcomes,
    decided entirely by the row:

    - A cancellation was pending: it takes effect now - at the end of the paid
      billing term, which on a yearly price is the end of the year.
    - A trial of a *priced* plan ended: `EXPIRED`, because nobody decided it. A
      free plan's "trial" is not a trial at all (BILL-01) - it simply rolls on.
    - A scheduled change applies: the next term opens on the new version and
      price - yearly to monthly takes effect here, and not a month earlier.
    - Otherwise the next term opens on the same terms. The subscription stays
      whatever it was - including `PAST_DUE`, since a new term does not
      settle an old debt.

    The new term ends at the next anniversary of the billing anchor, one term
    of the price on - a month or twelve calendar months - not one interval
    after the old end (BILL-18). A change of term length re-anchors at the
    boundary, because an anchor only means anything against one term. The new
    term's first monthly usage cycle opens with it (ADR-116).

    `term_price` is the price the opening term is *billed* at when that is not
    the subscription's own - a migration or a pricier operator change adopted
    only once paid. The term is as long as that price's, so a yearly renewal
    invoice always covers a year, even before its version is adopted.
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

    ending = _term_shape(plan, price)
    opening = ending
    start = subscription.current_period_end
    anchor = subscription.billing_anchor_at or legacy_anchor(subscription, *ending)
    if next_version is not None:
        subscription.plan_id = next_version.plan_id
        subscription.plan_version_id = next_version.id
        subscription.plan_price_id = next_price.id if next_price is not None else None
        opening = _term_shape(next_version, next_price)
    elif term_price is not None:
        opening = _term_shape(plan, term_price)
    if billing_calendar.term_months(*opening) != billing_calendar.term_months(*ending):
        anchor = start
    subscription.billing_anchor_at = anchor
    open_term(
        subscription,
        start=start,
        end=billing_calendar.next_boundary(anchor, start, *opening),
    )
    return subscription
