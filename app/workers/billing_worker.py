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
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import record_payment_reconciliation
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
from app.services.payment_reconciliation_service import PaymentReconciler
from app.services.recurring_service import MAX_COLLECTION_ATTEMPTS, RecurringService
from app.services.subscription_service import roll_over

logger = get_logger(__name__)

# Ten minutes. A period boundary is a date, not an instant, and sweeping harder
# would be querying constantly to learn nothing.
POLL_SECONDS: Final = 600.0

# How many rows one claim query takes. No longer a bound on what a sweep can
# do - `_drain` keeps claiming until a phase runs out of work - just the size of
# a bite. Each claimed row is processed in its own transaction, so a large batch
# does not mean a large transaction (ADR-082).
CLAIM_LIMIT: Final = 200

# How many batches one phase may take in a single pass. A guard, not a limit
# anybody should reach: every claim query excludes the state its own processing
# produces, so a row cannot be claimed twice in a pass. The bound exists so a
# future edit that breaks that property slows a sweep down instead of spinning
# one for ever.
MAX_BATCHES: Final = 200


class _Batch(Protocol):
    """One claim-and-process pass over a phase's eligible rows."""

    __name__: str

    def __call__(self, *, now: datetime) -> Awaitable[int]: ...


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
        """Work every subscription and invoice whose moment has come.

        Returns how much was done. Four phases, in an order that matters, and
        each phase drains rather than taking one batch: a first-of-the-month
        cohort rolls over in this pass instead of over the next several hours
        (ADR-082).

        **Every claim is its own transaction.** A claim is a row lock held until
        the transaction ends, so one transaction per sweep would put every
        claimed row behind the slowest one in the pass - including behind a
        Paymob request. One per claim also means a workspace that fails leaves
        every other workspace's work committed, which the single transaction
        could not promise.
        """
        moment = now or datetime.now(UTC)

        handled = await self._advance_due(now=moment)
        # Reconcile before collecting, and the order is the point. An attempt
        # whose answer never arrived makes its invoice uncollectible, so
        # resolving it first is what lets the same pass go on to charge - and
        # resolving it *after* would mean an invoice waits a full period for a
        # question that was answered ten milliseconds ago (ADR-088).
        handled += await self._reconcile(now=moment)
        # Collect before chasing. An invoice a saved card settles this sweep
        # should never also produce a past-due notice in the same pass;
        # charging first means the callback has a chance to arrive and the
        # chase sees an invoice that is already being dealt with.
        handled += await self._drain(self._collect_batch, now=moment)
        handled += await self._drain(self._chase_batch, now=moment)
        # Chase before suspending, and in that order for a reason: a workspace
        # whose invoice is already past the hard threshold when this worker
        # first sees it - because the loop was down, or because it was only
        # just issued the bill - is marked behind and suspended in the same
        # sweep, with both notices and both audit rows, rather than being cut
        # off having never been told.
        handled += await self._drain(self._suspend_batch, now=moment)

        logger.info("billing.sweep_completed", extra={"handled": handled})
        return handled

    async def _drain(self, batch: _Batch, *, now: datetime) -> int:
        """Run one phase until it stops finding work, or the bound is reached.

        A batch returning nothing ends the phase, and that is the correct
        reading of both possible causes: there is no eligible work left, or
        another worker holds what remains. Either way this worker is done.

        `MAX_BATCHES` is a guard rather than a limit anybody should reach. Every
        phase's claim query excludes what processing it produces, so a row
        cannot come back a second time - but that property lives in a query,
        and a future edit that breaks it should slow a sweep down rather than
        spin one for ever.
        """
        done = 0
        for pass_number in range(MAX_BATCHES):
            claimed = await batch(now=now)
            done += claimed
            if claimed == 0:
                return done
            if pass_number == MAX_BATCHES - 1:
                logger.warning(
                    "billing.sweep_batch_limit_reached",
                    extra={
                        "event": "billing.sweep_batch_limit_reached",
                        "phase": batch.__name__,
                        "batches": MAX_BATCHES,
                    },
                )
        return done

    async def _advance_due(self, *, now: datetime) -> int:
        """Roll over or expire every subscription whose period has ended."""
        return await self._drain(self._advance_batch, now=now)

    async def _advance_batch(self, *, now: datetime) -> int:
        """Claim a batch of due subscriptions, one transaction each."""
        async with self._database.session() as session:
            claimed = await PlatformSubscriptionRepository(session).claim_due(
                now=now,
                limit=self._claim_limit,
            )
            identifiers = [subscription.id for subscription in claimed]
        if not identifiers:
            return 0

        handled = 0
        for subscription_id in identifiers:
            handled += await self._advance_one(subscription_id, now=now)
        return handled

    async def _advance_one(self, subscription_id: uuid.UUID, *, now: datetime) -> int:
        """One subscription, claimed again inside its own transaction.

        Re-claimed rather than carried over from the batch: the batch's
        transaction has ended, so its lock is gone, and acting on the row
        without holding it is the duplicate this phase exists to prevent. The
        re-claim is by id and `SKIP LOCKED`, so a row another worker took in
        between is skipped rather than waited for or worked twice.
        """
        async with self._database.session() as session:
            subscriptions = PlatformSubscriptionRepository(session)
            subscription = await subscriptions.claim_by_id(subscription_id, now=now)
            if subscription is None:
                # Taken by another worker, or no longer due - a previous batch
                # in this same pass may already have rolled it.
                return 0

            plan = await PlanRepository(session).get_by_id(subscription.plan_id)
            if plan is None:
                # RESTRICT on the foreign key makes this unreachable, and it is
                # logged rather than crashed on: one impossible row must not
                # strand every other workspace's renewal behind it.
                logger.warning(
                    "billing.plan_missing_for_subscription",
                    extra={"subscription_id": str(subscription.id)},
                )
                return 0

            previous = subscription.status
            # Billed for the period that is ending, *before* it is rolled over:
            # after the roll the row's bounds describe the next month, and the
            # invoice would cover the wrong window.
            await self._invoice(session, subscription=subscription, plan=plan, now=now)
            await roll_over(subscription, plan=plan, now=now)
            if (
                previous is SubscriptionStatus.TRIALING
                and subscription.status is SubscriptionStatus.EXPIRED
            ):
                # The one transition nobody chose, so the owners are the last to
                # know unless they are told. Queued on this transaction, so the
                # notice and the expiry commit together (ADR-042).
                await self._notify_trial_expired(session, subscription=subscription)
            logger.info(
                "billing.subscription_advanced",
                extra={
                    "event": "billing.subscription_advanced",
                    "tenant_id": str(subscription.tenant_id),
                    "from_status": previous.value,
                    "status": subscription.status.value,
                },
            )
            return 1

    async def _reconcile(self, *, now: datetime) -> int:
        """Ask the provider about attempts whose answer never came back.

        A phase rather than a loop of its own, and deliberately: an unresolved
        attempt is a *billing* fact - it is the thing that makes an invoice
        uncollectible - so the sweep that decides what to collect is the sweep
        that should resolve it first. Splitting it out would mean the two ran
        on unrelated schedules and an invoice sat blocked for the difference.

        Not a queue job either, for the sharper reason the upload reconciler
        gives (ADR-087): a job naming an attempt to reconcile would be lost by
        exactly the failure it exists to recover from, because the process that
        would have enqueued it is the one that died. The committed row is the
        only record that survives, so a query over it is the only honest
        recovery.

        Silent and free when the deployment cannot ask - no provider, no
        inquiry credential, or a provider that has no such API. The attempts
        stay unresolved and visible rather than being guessed at.
        """
        provider = build_checkout_provider(self._settings)
        if provider is None:
            return 0

        async with self._database.session() as session:
            reconciler = PaymentReconciler(
                session=session,
                provider=provider,
                default_plan_code=self._settings.default_plan_code,
            )
            if not reconciler.available:
                return 0
            outcome = await reconciler.run(
                now=now,
                grace_seconds=self._settings.billing_reconciliation_grace_seconds,
                lease_seconds=self._settings.billing_reconciliation_lease_seconds,
                abandon_after_seconds=(self._settings.billing_reconciliation_abandon_after_seconds),
                limit=self._settings.billing_reconciliation_batch_size,
            )
            pending = await reconciler.unresolved_count()
            oldest = await reconciler.oldest_unresolved_seconds(now=now)

        await record_payment_reconciliation(
            settled=outcome.settled,
            failed=outcome.failed,
            abandoned=outcome.abandoned,
            still_pending=outcome.still_pending,
            not_found=outcome.not_found,
            unreachable=outcome.unreachable,
            pending=pending,
            oldest_pending_seconds=oldest,
        )

        if outcome.examined:
            logger.info(
                "billing.reconciliation_completed",
                extra={
                    "event": "billing.reconciliation_completed",
                    "settled": outcome.settled,
                    "failed": outcome.failed,
                    "abandoned": outcome.abandoned,
                    "still_pending": outcome.still_pending,
                    "not_found": outcome.not_found,
                    "unreachable": outcome.unreachable,
                    "pending": pending,
                },
            )
        return outcome.examined

    async def _collect_batch(self, *, now: datetime) -> int:
        """Take due renewals from saved cards, where that is possible at all.

        Silent and free when the provider cannot charge saved cards, which is
        the ordinary state: the capability is gated per merchant, and without
        it this returns immediately and renewals are collected by invoicing the
        customer exactly as before.

        Every refusal lives in `RecurringService`, not here. This is the query
        and the loop; what may be charged is a billing decision and belongs
        with the rules that make it.

        **One transaction per invoice, and the reason is the HTTP call inside
        it.** `collect` reaches Paymob, and a batch sharing a transaction would
        hold every claimed invoice's lock for the sum of every request in it.
        A worker sweeping alongside would find nothing to do and a customer's
        card would be charged while another workspace's row sat locked behind
        it (ADR-082).
        """
        provider = build_checkout_provider(self._settings)
        if provider is None or not isinstance(provider, RecurringProvider):
            return 0
        if not provider.can_charge_saved_methods:
            return 0

        async with self._database.session() as session:
            claimed = await PlatformInvoiceRepository(session).claim_collectible(
                before=now,
                max_attempts=MAX_COLLECTION_ATTEMPTS,
                limit=self._claim_limit,
            )
            identifiers = [invoice.id for invoice in claimed]
        if not identifiers:
            return 0

        charged = 0
        for invoice_id in identifiers:
            charged += await self._collect_one(invoice_id, provider=provider, now=now)
        return charged

    async def _collect_one(
        self,
        invoice_id: uuid.UUID,
        *,
        provider: RecurringProvider,
        now: datetime,
    ) -> int:
        """One collection attempt, holding only its own invoice.

        The invoice is claimed again by id, because the batch's lock ended with
        the batch's transaction. Claiming it here is what makes "only one worker
        reaches the provider for this attempt" true of the row rather than only
        of the payment row's unique key - which would catch a duplicate after
        the money had already moved.
        """
        async with self._database.session() as session:
            invoices = PlatformInvoiceRepository(session)
            invoice = await invoices.claim_by_id(
                invoice_id,
                max_attempts=MAX_COLLECTION_ATTEMPTS,
            )
            if invoice is None:
                return 0

            subscription = await PlatformSubscriptionRepository(session).get_by_id(
                invoice.subscription_id
            )
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
                # which is the same contract `_invoice` follows. The session
                # rolls back, so the claim is released without a charge.
                logger.exception(
                    "billing.recurring_collection_failed",
                    extra={"invoice_id": str(invoice.id)},
                )
                return 0

            if outcome.charged:
                return 1
            if outcome.reason is not None:
                logger.info(
                    "billing.recurring_skipped",
                    extra={
                        "event": "billing.recurring_skipped",
                        "invoice_id": str(invoice.id),
                        "reason": outcome.reason,
                    },
                )
            return 0

    async def _chase_batch(self, *, now: datetime) -> int:
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
        return await self._dun(
            now=now,
            grace=timedelta(days=self._grace_days),
            require=SubscriptionStatus.ACTIVE,
            become=SubscriptionStatus.PAST_DUE,
        )

    async def _suspend_batch(self, *, now: datetime) -> int:
        """Stop serving a workspace whose bill has gone unpaid past the grace.

        The half of dunning that was missing (ADR-061). `_chase_batch` above
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

        Only a `PAST_DUE` subscription is suspended, and that is now the claim
        query's predicate rather than a check afterwards. It is not merely an
        ordering convenience: it means a workspace is never cut off without the
        state and the notice that precede it, even when both happen in one
        sweep because this loop had been down.

        The threshold is read from `issued_at`, exactly as the soft one is, so
        both are anchored to the day the customer was asked for money and
        neither can move under a workspace being chased.

        **A workspace whose last collection attempt has no outcome is not
        suspended** (ADR-088). The audit named two harms and this is the
        second: a customer whose card was debited by a worker that then died,
        whose callback never arrived, cut off for non-payment. Reconciliation
        normally resolves such an attempt within one sweep, so this guard fires
        only when it cannot - a deployment with no `PAYMOB_API_KEY`, or a
        provider that has been unreachable for a month - and the safety
        property should not depend on optional configuration.

        The cost is stated rather than hidden: an attempt nobody can ever
        resolve keeps a workspace served. That is why the backlog is a metric
        with an alert on its age (`wasla_oldest_pending_payment_age_seconds`)
        rather than something only a support ticket would surface. Chasing is
        not guarded, so such a workspace is still marked behind and still told.
        """
        return await self._dun(
            now=now,
            grace=timedelta(days=self._suspend_after_days),
            require=SubscriptionStatus.PAST_DUE,
            become=SubscriptionStatus.SUSPENDED,
            skip_unresolved=True,
        )

    async def _dun(
        self,
        *,
        now: datetime,
        grace: timedelta,
        require: SubscriptionStatus,
        become: SubscriptionStatus,
        skip_unresolved: bool = False,
    ) -> int:
        """Claim one batch of overdue invoices and move each subscription on.

        Both dunning phases are this function with different thresholds and
        different endpoints, which is what they always were - the duplication
        that used to sit between them was two copies of a claim loop.

        `require` is the state a subscription must be in, and it is in the
        *query* (ADR-082). Processing an invoice changes its subscription and
        leaves the invoice as overdue as it was, so a status checked after the
        claim left the row eligible for ever, holding its place at the front of
        every later batch. More than `claim_limit` already-processed invoices
        and the ones behind them were never reached.
        """
        async with self._database.session() as session:
            claimed = await PlatformInvoiceRepository(session).claim_overdue(
                before=now - grace,
                subscription_status=require,
                limit=self._claim_limit,
                skip_unresolved=skip_unresolved,
            )
            identifiers = [(invoice.id, subscription.id) for invoice, subscription in claimed]
        if not identifiers:
            return 0

        moved = 0
        for invoice_id, subscription_id in identifiers:
            moved += await self._dun_one(
                invoice_id,
                subscription_id,
                now=now,
                grace=grace,
                require=require,
                become=become,
                skip_unresolved=skip_unresolved,
            )
        return moved

    async def _dun_one(
        self,
        invoice_id: uuid.UUID,
        subscription_id: uuid.UUID,
        *,
        now: datetime,
        grace: timedelta,
        require: SubscriptionStatus,
        become: SubscriptionStatus,
        skip_unresolved: bool = False,
    ) -> int:
        """One transition, its audit row and its notice, in one transaction.

        Everything is staged on this session - the status, the audit row and the
        outbox row - so a suspension and the message telling somebody about it
        commit together or not at all (ADR-042). What changed is only that the
        transaction is per workspace rather than per sweep, so one workspace's
        failure no longer takes the pass with it.

        The pair is claimed again by id. The batch's locks ended with the
        batch's transaction, and `require` is re-checked by the claim itself -
        so a subscription some other worker moved in between is skipped rather
        than transitioned twice.
        """
        async with self._database.session() as session:
            claimed = await PlatformInvoiceRepository(session).claim_overdue_pair(
                invoice_id=invoice_id,
                subscription_id=subscription_id,
                before=now - grace,
                subscription_status=require,
                skip_unresolved=skip_unresolved,
            )
            if claimed is None:
                return 0
            invoice, subscription = claimed

            subscription.status = become
            if become is SubscriptionStatus.SUSPENDED:
                # `ended_at` is deliberately left alone. It records a
                # subscription that finished, and this one has not: it is
                # waiting for a payment that lifts it
                # (`CheckoutService._settle`).
                action = AuditAction.SUBSCRIPTION_SUSPENDED
                template = EmailTemplate.SUBSCRIPTION_SUSPENDED
                prefix = f"subscription-suspended:{invoice.id}"
                meta: dict[str, object] = {
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                    "currency": invoice.currency,
                    "unpaid_days": self._suspend_after_days,
                }
                tenant = await TenantRepository(session).get_by_id(subscription.tenant_id)
                context = {
                    "workspace_name": tenant.name if tenant is not None else "your workspace",
                    "amount_due": f"{invoice.outstanding:.2f}",
                    "currency": invoice.currency,
                }
            else:
                action = AuditAction.SUBSCRIPTION_PAST_DUE
                # The same template as the bill itself: a past-due notice is
                # that bill again, said louder.
                template = EmailTemplate.INVOICE_ISSUED
                prefix = f"invoice-overdue:{invoice.id}"
                meta = {
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                    "currency": invoice.currency,
                }
                context = {
                    "amount_due": f"{invoice.outstanding:.2f}",
                    "currency": invoice.currency,
                    "period_start": invoice.period_start.date().isoformat(),
                    "period_end": invoice.period_end.date().isoformat(),
                }

            AuditTrail(session, tenant_id=subscription.tenant_id).record(
                action,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="subscription",
                target_id=subscription.id,
                meta=meta,
            )
            # Keyed to the invoice, so a workspace that stays behind is told
            # once about this bill rather than once every sweep.
            await EmailOutbox(session, self._settings).enqueue_for_tenant_owners(
                tenant_id=subscription.tenant_id,
                template=template,
                idempotency_prefix=prefix,
                context=context,
            )
            logger.warning(
                (
                    "billing.subscription_suspended"
                    if become is SubscriptionStatus.SUSPENDED
                    else "billing.subscription_past_due"
                ),
                extra={
                    "event": (
                        "billing.subscription_suspended"
                        if become is SubscriptionStatus.SUSPENDED
                        else "billing.subscription_past_due"
                    ),
                    "tenant_id": str(subscription.tenant_id),
                    "invoice_id": str(invoice.id),
                    "outstanding": str(invoice.outstanding),
                },
            )
            return 1

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
