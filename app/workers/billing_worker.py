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
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import Plan, Subscription, SubscriptionStatus
from app.db.session import Database
from app.integrations.billing import build_checkout_provider
from app.integrations.billing.checkout import RecurringProvider
from app.repositories.billing_repository import (
    PlanRepository,
    PlatformSubscriptionRepository,
)
from app.repositories.invoice_repository import PlatformInvoiceRepository
from app.repositories.tenant_repository import TenantRepository
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate
from app.services.invoice_service import InvoiceService
from app.services.recurring_service import RecurringService
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

# Both dunning thresholds are configuration (ADR-061). These names survive as
# the defaults a caller gets when it constructs the worker without settings,
# which is what the older tests do; `runner.py` builds it from `Settings`, so a
# deployment changes the numbers rather than the code.
GRACE_DAYS: Final = 7
SUSPEND_AFTER_DAYS: Final = 30


class BillingWorker:
    """Polls for subscriptions whose period has ended and advances them."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        poll_seconds: float = POLL_SECONDS,
        claim_limit: int = CLAIM_LIMIT,
        grace_days: int | None = None,
        suspend_after_days: int | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._claim_limit = claim_limit
        # Settings decide unless a caller is explicit. A test driving a
        # boundary wants to name its own thresholds; a deployment wants the
        # ones an operator configured, and `Settings` has already refused a
        # pair where the hard threshold is not strictly later than the soft.
        self._grace_days = grace_days if grace_days is not None else settings.billing_past_due_days
        self._suspend_after_days = (
            suspend_after_days
            if suspend_after_days is not None
            else settings.billing_suspend_after_days
        )
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
                # Billed for the period that is ending, *before* it is rolled
                # over: after the roll the row's bounds describe the next month,
                # and the invoice would cover the wrong window.
                await self._invoice(
                    session,
                    subscription=subscription,
                    plan=plan,
                    now=moment,
                )
                await roll_over(subscription, plan=plan, now=moment)
                if (
                    previous is SubscriptionStatus.TRIALING
                    and subscription.status is SubscriptionStatus.EXPIRED
                ):
                    # The one transition nobody chose, so the owners are the
                    # last to know unless they are told. Queued on the sweep's
                    # own session, so the notice and the expiry commit
                    # together (ADR-042).
                    await self._notify_trial_expired(session, subscription=subscription)
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

            # Collect before chasing. An invoice a saved card settles this
            # sweep should never also produce a past-due notice in the same
            # pass; charging first means the callback has a chance to arrive
            # and the chase sees an invoice that is already being dealt with.
            handled += await self._collect_renewals(session, now=moment)
            handled += await self._chase_unpaid(session, now=moment)
            # Chase before suspending, and in that order for a reason: a
            # workspace whose invoice is already past the hard threshold when
            # this worker first sees it - because the loop was down, or because
            # it was only just issued the bill - is marked behind and suspended
            # in the same sweep, with both notices and both audit rows, rather
            # than being cut off having never been told.
            handled += await self._suspend_unpaid(session, now=moment)

        logger.info("billing.sweep_completed", extra={"handled": handled})
        return handled

    async def _collect_renewals(self, session: AsyncSession, *, now: datetime) -> int:
        """Take due renewals from saved cards, where that is possible at all.

        Silent and free when the provider cannot charge saved cards, which is
        the ordinary state: the capability is gated per merchant, and without
        it this returns immediately and renewals are collected by invoicing the
        customer exactly as before.

        Every refusal lives in `RecurringService`, not here. This is the query
        and the loop; what may be charged is a billing decision and belongs
        with the rules that make it.
        """
        provider = build_checkout_provider(self._settings)
        if provider is None or not isinstance(provider, RecurringProvider):
            return 0
        if not provider.can_charge_saved_methods:
            return 0

        invoices = await PlatformInvoiceRepository(session).collectible(
            before=now,
            limit=self._claim_limit,
        )
        if not invoices:
            return 0

        subscriptions = PlatformSubscriptionRepository(session)
        charged = 0
        for invoice in invoices:
            subscription = await subscriptions.get_by_id(invoice.subscription_id)
            service = RecurringService(
                session,
                tenant_id=invoice.tenant_id,
                provider=provider,
            )
            try:
                outcome = await service.collect(invoice, subscription=subscription, now=now)
            except Exception:
                # One workspace's provider trouble must not strand every other
                # renewal behind it. Logged loudly and left for the next sweep,
                # which is the same contract `_invoice` follows.
                logger.exception(
                    "billing.recurring_collection_failed",
                    extra={"invoice_id": str(invoice.id)},
                )
                continue

            if outcome.charged:
                charged += 1
            elif outcome.reason is not None:
                logger.info(
                    "billing.recurring_skipped",
                    extra={
                        "event": "billing.recurring_skipped",
                        "invoice_id": str(invoice.id),
                        "reason": outcome.reason,
                    },
                )
        return charged

    async def _chase_unpaid(self, session: AsyncSession, *, now: datetime) -> int:
        """Mark workspaces behind when a renewal has gone unpaid for too long.

        This is the half of recurring billing that is not about issuing a bill.
        Invoices were already produced at every period end and nothing ever
        looked at whether they were paid, so a workspace that stopped paying
        stayed `active` for ever and kept its full plan - which is a product
        given away rather than a payment problem.

        `PAST_DUE` and not `CANCELLED`, deliberately. It is a state that still
        serves the customer (`SERVING_STATUSES` includes it) and is the
        conversation to have before cutting anybody off; a first failed card
        should not end a relationship. When the invoice is paid, the callback
        puts the subscription back to `ACTIVE` - see `CheckoutService._settle`,
        which is the only thing that does.

        Grace runs from `issued_at`, so somebody has the configured number of
        days from being *asked* rather than from a period boundary they never
        saw.
        """
        grace = timedelta(days=self._grace_days)
        invoices = await PlatformInvoiceRepository(session).overdue(
            before=now - grace,
            limit=self._claim_limit,
        )
        if not invoices:
            return 0

        subscriptions = PlatformSubscriptionRepository(session)
        marked = 0
        for invoice in invoices:
            subscription = await subscriptions.get_by_id(invoice.subscription_id)
            if subscription is None or subscription.status is not SubscriptionStatus.ACTIVE:
                # Trials owe nothing, and a cancelled or expired subscription
                # is already not being served. Only an active workspace can
                # fall behind.
                continue

            subscription.status = SubscriptionStatus.PAST_DUE
            AuditTrail(session, tenant_id=subscription.tenant_id).record(
                AuditAction.SUBSCRIPTION_PAST_DUE,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="subscription",
                target_id=subscription.id,
                meta={
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                    "currency": invoice.currency,
                },
            )
            # Keyed to the invoice, so a workspace that stays behind is told
            # once about this bill rather than once every sweep.
            await EmailOutbox(session, self._settings).enqueue_for_tenant_owners(
                tenant_id=subscription.tenant_id,
                template=EmailTemplate.INVOICE_ISSUED,
                idempotency_prefix=f"invoice-overdue:{invoice.id}",
                context={
                    "amount_due": f"{invoice.outstanding:.2f}",
                    "currency": invoice.currency,
                    "period_start": invoice.period_start.date().isoformat(),
                    "period_end": invoice.period_end.date().isoformat(),
                },
            )
            marked += 1
            logger.warning(
                "billing.subscription_past_due",
                extra={
                    "event": "billing.subscription_past_due",
                    "tenant_id": str(subscription.tenant_id),
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                },
            )
        return marked

    async def _suspend_unpaid(self, session: AsyncSession, *, now: datetime) -> int:
        """Stop serving a workspace whose bill has gone unpaid past the grace.

        The half of dunning that was missing (ADR-061). `_chase_unpaid` above
        moved a workspace to `PAST_DUE` and nothing moved it anywhere after
        that - and `PAST_DUE` is a *serving* status, so a workspace that simply
        stopped paying kept its paid plan for ever. The purchase was protected
        by ADR-059 and the retention was not, which is the same product given
        away by a slower route.

        `SUSPENDED` rather than `CANCELLED`, and that distinction is the whole
        reason a new status exists. A cancellation is the customer's decision;
        this is the platform's. Recording it as the former would misattribute
        it in the trail, count it as churn on a dashboard that separates
        cancellations from failed payments, and - because paying an invoice
        deliberately does not revive a subscription somebody chose to end -
        make recovery impossible to express.

        Only a `PAST_DUE` subscription is suspended. That is not merely an
        ordering convenience: it means a workspace is never cut off without the
        state and the notice that precede it, even when both happen in one
        sweep because this loop had been down.

        The threshold is read from `issued_at`, exactly as the soft one is, so
        both are anchored to the day the customer was asked for money and
        neither can move under a workspace being chased.

        Everything here is staged on the sweep's session - the status, the
        audit row and the outbox row - so a suspension and the message telling
        somebody about it commit together or not at all (ADR-042).
        """
        grace = timedelta(days=self._suspend_after_days)
        invoices = await PlatformInvoiceRepository(session).overdue(
            before=now - grace,
            limit=self._claim_limit,
        )
        if not invoices:
            return 0

        subscriptions = PlatformSubscriptionRepository(session)
        suspended = 0
        for invoice in invoices:
            subscription = await subscriptions.get_by_id(invoice.subscription_id)
            if subscription is None or subscription.status is not SubscriptionStatus.PAST_DUE:
                # Already suspended by an earlier sweep, already cancelled, or
                # never chased. Re-running this method is therefore a no-op,
                # which is what makes the sweep safe to repeat: the status is
                # the claim, and only one pass can find it in `PAST_DUE`.
                continue

            subscription.status = SubscriptionStatus.SUSPENDED
            # `ended_at` is deliberately left alone. It records a subscription
            # that finished, and this one has not: it is waiting for a payment
            # that lifts it (`CheckoutService._settle`).
            AuditTrail(session, tenant_id=subscription.tenant_id).record(
                AuditAction.SUBSCRIPTION_SUSPENDED,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="subscription",
                target_id=subscription.id,
                meta={
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                    "currency": invoice.currency,
                    "unpaid_days": self._suspend_after_days,
                },
            )
            tenant = await TenantRepository(session).get_by_id(subscription.tenant_id)
            # Keyed to the invoice, like the past-due notice, so a workspace
            # that stays suspended is told once about this bill rather than
            # once every ten minutes for ever.
            await EmailOutbox(session, self._settings).enqueue_for_tenant_owners(
                tenant_id=subscription.tenant_id,
                template=EmailTemplate.SUBSCRIPTION_SUSPENDED,
                idempotency_prefix=f"subscription-suspended:{invoice.id}",
                context={
                    "workspace_name": tenant.name if tenant is not None else "your workspace",
                    "amount_due": f"{invoice.outstanding:.2f}",
                    "currency": invoice.currency,
                },
            )
            suspended += 1
            logger.warning(
                "billing.subscription_suspended",
                extra={
                    "event": "billing.subscription_suspended",
                    "tenant_id": str(subscription.tenant_id),
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                    "unpaid_days": self._suspend_after_days,
                },
            )
        return suspended

    async def _invoice(
        self,
        session: AsyncSession,
        *,
        subscription: Subscription,
        plan: Plan,
        now: datetime,
    ) -> None:
        """Bill the period that has just ended, if it should be billed.

        A trial is not invoiced. Nobody agreed to pay for it, and an invoice
        saying "Pro plan" for a period the customer was told was free is a bill
        for something nobody sold.

        Failures are contained. An invoice that could not be issued is worth a
        loud log and a retry on the next sweep; letting it escape would stop the
        subscription rolling over at all, turning a billing problem into a
        customer whose plan never renews.
        """
        if subscription.status is SubscriptionStatus.TRIALING:
            return

        service = InvoiceService(session, tenant_id=subscription.tenant_id)
        try:
            invoice, created = await service.issue_for_period(
                subscription=subscription,
                plan=plan,
                period_start=subscription.current_period_start,
                period_end=subscription.current_period_end,
                now=now,
            )
        except Exception:
            logger.exception(
                "billing.invoice_failed",
                extra={"subscription_id": str(subscription.id)},
            )
            return

        if created:
            # Keyed to the invoice row, so a sweep that runs twice over the
            # same period notifies once. No payment link: the template says so
            # and means it, because a bill with a link in it is the shape every
            # invoice-phishing email takes.
            await EmailOutbox(session, self._settings).enqueue_for_tenant_owners(
                tenant_id=subscription.tenant_id,
                template=EmailTemplate.INVOICE_ISSUED,
                idempotency_prefix=f"invoice-issued:{invoice.id}",
                context={
                    # Numeric(12, 2) already, so it is formatted rather than
                    # scaled: money is never divided on its way into a notice.
                    "amount_due": f"{invoice.amount_due:.2f}",
                    "currency": invoice.currency,
                    "period_start": invoice.period_start.date().isoformat(),
                    "period_end": invoice.period_end.date().isoformat(),
                },
            )
            logger.info(
                "billing.invoice_issued_by_sweep",
                extra={
                    "event": "billing.invoice_issued_by_sweep",
                    "tenant_id": str(subscription.tenant_id),
                    "invoice_id": str(invoice.id),
                },
            )

    async def _notify_trial_expired(
        self,
        session: AsyncSession,
        *,
        subscription: Subscription,
    ) -> None:
        """Tell a workspace's owners that its trial has ended.

        Keyed to the subscription rather than the moment: a trial expires
        once, so a sweep replayed against the same row must not send twice.
        """
        tenant = await TenantRepository(session).get_by_id(subscription.tenant_id)
        await EmailOutbox(session, self._settings).enqueue_for_tenant_owners(
            tenant_id=subscription.tenant_id,
            template=EmailTemplate.TRIAL_EXPIRED,
            idempotency_prefix=f"trial-expired:{subscription.id}",
            context={"workspace_name": tenant.name if tenant is not None else "your workspace"},
        )


__all__ = ["CLAIM_LIMIT", "POLL_SECONDS", "BillingWorker"]
