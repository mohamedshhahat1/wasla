"""One settlement engine: money arriving against an invoice, whoever reports it.

Three routes report money, and before this module they did not agree (BILL-10,
BILL-20). A verified provider callback settled through `CheckoutService._settle`,
which restored a suspended workspace and granted a purchased plan; a platform
operator recording a bank transfer went through `InvoiceService._settle`, which
did neither, skipped the invoice transition table, and could pay an invoice that
was already paid. A reconciliation inquiry reused the first. So the same money
had two meanings depending on who noticed it.

Now every route calls `InvoiceSettlement.settle`, and it decides three things in
one place:

1. **May this invoice take this money?** Only an `OPEN` invoice, through the
   transition table. Money for a paid, voided or written-off invoice - a second
   payment page paid, a withdrawn bill paid late - is recorded on the payment
   and refused by the invoice, and becomes a durable **billing incident** rather
   than a log line (BILL-15). An operator decides whether to refund it.
2. **Does the purchase still make sense?** A checkout is a customer's choice,
   frozen when they made it: plan version and the exact price of it - amount,
   currency, billing term (BILL-06, ADR-116). Its settlement grants exactly
   that version, never whatever the invoice might have been re-pointed at -
   invoices are never re-pointed any more. It is refused (money held,
   incident raised) only where granting would be wrong: the customer
   cancelled *after* opening the page, or already holds the same plan on the
   same billing term, paid for this period.
3. **What does it grant?** A purchase starts a new paid term *at settlement*
   - a month or a year, as its price says - and re-anchors the subscription
   there, with its first monthly usage cycle (BILL-03, ADR-116); a terminal
   subscription the customer chose to buy back is reactivated (BILL-01); a
   paid renewal lifts `PAST_DUE` or `SUSPENDED` and adopts the version and
   price it was issued for.

**One lock order** (DB-001, DB-003). Two settlements of one invoice used to
read it unlocked, each compute `0 + 99`, and both apply: two succeeded
payments, an invoice saying 99 was paid, and no incident. And two settlements
touching the same payment took the payment, invoice and subscription rows in
whatever order their flushes happened to write them, and deadlocked. Every
settlement now takes its rows up front, in one order, before deciding
anything:

    payment -> invoice -> subscription -> offer / top-up

`lock` takes the first three; the offer and top-up ledgers lock their own
rows afterwards. The refusal decision is made on the invoice as it is
*after* the lock, so a second payment for an invoice another transaction has
just paid is refused and raised as a `duplicate_payment` incident - exactly
what happens when the two arrive one after the other.

Beneath that, the database keeps the books honest on its own (migration
0074): an invoice's `amount_paid` must equal the net of the payments applied
to it (`payments.applied_at`), and a collected payment that was *not* applied
must be explained by an incident. A settlement path that forgot the lock
would fail at commit rather than record money twice.

Nothing here talks to a provider, and nothing here can move money.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    CustomPlanNotAvailableError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import (
    PlanPrice,
    PlanVersion,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.billing_incident import BillingIncidentKind
from app.db.models.channel_capacity import CapacityReductionCause
from app.db.models.invoice import (
    UNRESOLVED_COLLECTION_STATES,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    invoice_may_move,
)
from app.db.models.user import User
from app.integrations.billing.checkout import CallbackEvent
from app.repositories.billing_repository import (
    BillingAdjustmentRepository,
    PlanRepository,
    SubscriptionRepository,
)
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.services.audit_service import AuditTrail
from app.services.billing_incident_service import raise_incident
from app.services.capacity_reduction import ChannelCapacityReductions
from app.services.custom_plan_offer_ledger import CustomPlanOfferLedger
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from app.services.topup_ledger import TopupLedger

logger = get_logger(__name__)

# What a settlement did, in one word - the same vocabulary the callback ledger
# records (`payment_events.outcome`).
APPLIED: Final = "applied"
DUPLICATE: Final = "duplicate"
UNMATCHED: Final = "unmatched"
MISMATCHED: Final = "mismatched"
NO_CHANGE: Final = "no_change"
REFUSED: Final = "refused"
# A hosted checkout's transaction was declined, and the page may still be paid
# by another transaction on the same order (BILL-07). History, not a verdict.
DECLINED: Final = "declined"

# The two subscription statuses a settled renewal lifts (ADR-059, ADR-061).
RECOVERABLE_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {SubscriptionStatus.PAST_DUE, SubscriptionStatus.SUSPENDED}
)

# Invoices a purchase buys a plan with.
PURCHASE_PURPOSES: Final[frozenset[InvoicePurpose]] = frozenset(
    {InvoicePurpose.CHECKOUT, InvoicePurpose.MANUAL}
)


@dataclass(frozen=True, slots=True)
class _Refusal:
    kind: BillingIncidentKind
    detail: str


@dataclass(frozen=True, slots=True)
class SettlementRows:
    """The rows one settlement holds locked, re-read after locking."""

    payment: Payment | None
    invoice: Invoice
    subscription: Subscription | None


class InvoiceSettlement:
    """Applies money to one workspace's invoices, and what paying them grants."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider_name: str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._provider_name = provider_name
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._catalog = PlanCatalog(session)
        self._adjustments = BillingAdjustmentRepository(session)
        self._audit = AuditTrail(session, tenant_id=tenant_id)
        self._offers = CustomPlanOfferLedger(session, tenant_id=tenant_id)

    # ------------------------------------------------------------ locking

    async def lock(
        self,
        *,
        invoice_id: uuid.UUID,
        payment_id: uuid.UUID | None = None,
    ) -> SettlementRows:
        """Take the settlement locks in the one global order, and re-read.

        Payment, then invoice, then the workspace's subscription - the same
        order for a callback, a reconciliation, a manual payment and a
        refund, so two of them can wait for each other but never deadlock
        (DB-003). Each row is re-read under its lock, so what is decided
        next is decided on committed state rather than on a copy read
        before another settlement finished (DB-001).

        Call it before changing any of the three rows: re-reading discards
        unflushed changes. Taking a lock this transaction already holds is a
        no-op, so a caller that locked early and `settle` locking again is
        the intended shape, not a double lock.
        """
        payment: Payment | None = None
        if payment_id is not None:
            payment = await PaymentRepository(self._session, tenant_id=self._tenant_id).lock(
                payment_id
            )
        invoice = await self._invoices.lock(invoice_id)
        if invoice is None:
            raise NotFoundError("No such invoice.")
        subscription = await self._subscriptions.lock()
        return SettlementRows(payment=payment, invoice=invoice, subscription=subscription)

    # ------------------------------------------------------------ settling

    async def settle(
        self,
        invoice: Invoice,
        *,
        payment: Payment,
        now: datetime,
        actor: User | None = None,
        recover_uncollectible: bool = False,
    ) -> tuple[str, str | None]:
        """Apply one collected payment to its invoice. See the module docstring.

        `recover_uncollectible` is the deliberate operator recovery that alone
        may pay an invoice written off as uncollectible (spec: invoice state
        machine). A provider callback never passes it.
        """
        # Whatever the caller already holds, the decision below is made under
        # the locks and on the rows as they are now. The flush first: the
        # payment may be new or carry the provider's outcome, and re-reading
        # it must not discard that.
        await self._session.flush()
        rows = await self.lock(invoice_id=invoice.id, payment_id=payment.id)
        subscription = rows.subscription
        refusal = self._invoice_refusal(
            invoice, payment=payment, recover_uncollectible=recover_uncollectible
        )
        if refusal is not None:
            return await self._refuse(invoice, payment=payment, refusal=refusal, now=now)

        version: PlanVersion | None = None
        price: PlanPrice | None = None
        keep_cancellation = False
        completes = invoice.amount_paid + payment.amount >= invoice.amount_due
        if invoice.purpose in PURCHASE_PURPOSES and completes:
            version, price = await self._purchased_terms(invoice)
            if version is not None:
                decision = await self._purchase_refusal(
                    invoice, version=version, subscription=subscription, now=now
                )
                if decision is None:
                    offer_problem = await self._offers.refusal(invoice)
                    if offer_problem is not None:
                        decision = _Refusal(BillingIncidentKind.REFUSED_SETTLEMENT, offer_problem)
                if decision is not None:
                    return await self._refuse(invoice, payment=payment, refusal=decision, now=now)
                keep_cancellation = self._cancelled_after_opening(invoice, subscription)

        invoice.amount_paid = invoice.amount_paid + payment.amount
        # The money is now on the invoice: the ledger's record that this
        # payment, and not a held duplicate, is what `amount_paid` counts.
        payment.applied_at = now
        if payment.provider_reference:
            invoice.provider_reference = payment.provider_reference
        if invoice.amount_paid >= invoice.amount_due:
            if version is not None:
                # Before the invoice is marked paid: the grant writes the
                # period the payment opened onto the invoice, and a paid
                # invoice's terms are frozen by the database (DB-005).
                await self._grant(
                    invoice,
                    version=version,
                    price=price,
                    keep_cancellation=keep_cancellation,
                    now=now,
                )
            self._move(invoice, InvoiceStatus.PAID)
            invoice.paid_at = now

        if invoice.status is InvoiceStatus.PAID:
            if version is not None:
                await self._offers.activated(invoice, now=now)
            elif invoice.purpose is InvoicePurpose.TOPUP:
                # Extra allowance, never a plan: the subscription is left
                # exactly as it is, whatever the invoice's other fields say
                # (ADR-113).
                await TopupLedger(self._session, tenant_id=self._tenant_id).invoice_paid(
                    invoice, payment=payment, now=now
                )
            elif invoice.purpose not in PURCHASE_PURPOSES:
                await self.renewal_paid(invoice, subscription=subscription, now=now)

        self._audit.record(
            AuditAction.PAYMENT_RECORDED,
            actor=actor,
            actor_kind=(
                AuditActorKind.PLATFORM_STAFF if actor is not None else AuditActorKind.SYSTEM
            ),
            target_type="invoice",
            target_id=invoice.id,
            meta={
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": invoice.currency,
                "provider": payment.provider,
                "purpose": invoice.purpose.value,
                "invoice_status": invoice.status.value,
            },
        )
        logger.info(
            "billing.payment_applied",
            extra={
                "event": "billing.payment_applied",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "status": invoice.status.value,
            },
        )
        return APPLIED, f"Invoice {invoice.status.value}."

    def _invoice_refusal(
        self,
        invoice: Invoice,
        *,
        payment: Payment,
        recover_uncollectible: bool,
    ) -> _Refusal | None:
        """Why this invoice cannot take this money, or None."""
        if invoice.status is InvoiceStatus.PAID:
            return _Refusal(
                BillingIncidentKind.DUPLICATE_PAYMENT,
                "Money arrived for an invoice that was already paid.",
            )
        if invoice.status is InvoiceStatus.VOID:
            return _Refusal(
                BillingIncidentKind.REFUSED_SETTLEMENT,
                "Money arrived for a voided invoice.",
            )
        if invoice.status is InvoiceStatus.UNCOLLECTIBLE and not recover_uncollectible:
            return _Refusal(
                BillingIncidentKind.REFUSED_SETTLEMENT,
                "Money arrived for an invoice written off as uncollectible.",
            )
        if invoice.status is InvoiceStatus.DRAFT:
            return _Refusal(
                BillingIncidentKind.REFUSED_SETTLEMENT,
                "Money arrived for an invoice that was never issued.",
            )
        if payment.currency.upper() != invoice.currency.upper():
            return _Refusal(
                BillingIncidentKind.MISMATCHED_CALLBACK,
                f"Paid in {payment.currency}, invoiced in {invoice.currency}.",
            )
        if payment.amount > invoice.outstanding:
            # More than is owed. `amount_paid <= amount_due` is a database
            # constraint now, and the excess is somebody's money that no
            # invoice can hold (BILL-15).
            return _Refusal(
                BillingIncidentKind.DUPLICATE_PAYMENT,
                f"Paid {payment.amount} against {invoice.outstanding} outstanding.",
            )
        return None

    async def _refuse(
        self,
        invoice: Invoice,
        *,
        payment: Payment,
        refusal: _Refusal,
        now: datetime,
    ) -> tuple[str, str | None]:
        """Keep the money on the payment, leave the invoice, and tell somebody."""
        kind = refusal.kind
        if (
            invoice.purpose is InvoicePurpose.TOPUP
            and kind is BillingIncidentKind.DUPLICATE_PAYMENT
        ):
            kind = BillingIncidentKind.TOPUP_DUPLICATE_PAYMENT
        await raise_incident(
            self._session,
            kind=kind,
            dedupe_key=f"{payment.id}:{payment.provider_reference or ''}",
            tenant_id=self._tenant_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            provider=payment.provider,
            provider_transaction_id=payment.provider_reference,
            amount=payment.amount,
            currency=payment.currency,
            detail=refusal.detail,
            now=now,
        )
        logger.warning(
            "billing.settlement_refused",
            extra={
                "event": "billing.settlement_refused",
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "status": invoice.status.value,
                "kind": kind.value,
            },
        )
        return REFUSED, refusal.detail

    # ------------------------------------------------------------ purchases

    async def _purchased_terms(
        self, invoice: Invoice
    ) -> tuple[PlanVersion | None, PlanPrice | None]:
        """The frozen terms this invoice sells: its version and its price.

        Always the invoice's own - never the plan's current price, which may
        have been retired or replaced since the page was opened (ADR-116). An
        invoice written before versioning names only a plan code; it is
        treated as buying that plan's current version at the price matching
        what it charged, which is what it would have granted before. None when
        the plan no longer exists.
        """
        if invoice.plan_version_id is not None:
            version = await self._catalog.get_version(invoice.plan_version_id)
        else:
            plan = await self._plans.get_by_code(invoice.plan_code)
            version = await self._catalog.current_version(plan) if plan is not None else None
        if version is None or version.is_free:
            return version, None
        if invoice.plan_price_id is not None:
            return version, await self._catalog.get_price(invoice.plan_price_id)
        for candidate in await self._catalog.prices(version, active_only=False):
            if candidate.amount == invoice.amount_due and candidate.currency == invoice.currency:
                return version, candidate
        return version, await self._catalog.default_price(version)

    async def _purchase_refusal(
        self,
        invoice: Invoice,
        *,
        version: PlanVersion,
        subscription: Subscription | None,
        now: datetime,
    ) -> _Refusal | None:
        """Why granting this purchase would be wrong, or None to grant it."""
        try:
            await self._catalog.require_available(version, tenant_id=self._tenant_id)
        except CustomPlanNotAvailableError:
            # Another workspace's custom plan. The trigger on `invoices` makes
            # such an invoice impossible to write; if one existed anyway, its
            # money is held and an operator told, never the plan granted.
            return _Refusal(
                BillingIncidentKind.CUSTOM_PLAN_SCOPE_MISMATCH,
                "The invoice buys a custom plan that belongs to another workspace.",
            )
        if subscription is None:
            return None
        if subscription.status is SubscriptionStatus.CANCELLED:
            if self._cancelled_after_opening(invoice, subscription):
                # The customer ended the subscription after opening this page.
                # Paying the page afterwards is not an instruction to undo that
                # (spec: callbacks after cancellation), so the money waits for
                # an operator rather than reviving what they ended.
                return _Refusal(
                    BillingIncidentKind.REFUSED_SETTLEMENT,
                    "The subscription was cancelled after this checkout was opened.",
                )
            return None
        if (
            subscription.is_serving
            and subscription.plan_id == version.plan_id
            and not await self._lengthens_term(invoice, subscription)
            and (
                await self._invoices.has_other_settled_cover(
                    invoice_id=invoice.id, plan_code=invoice.plan_code, at=now
                )
                or await self._adjustments.covering(subscription_id=subscription.id, at=now)
            )
        ):
            return _Refusal(
                BillingIncidentKind.DUPLICATE_PAYMENT,
                "The workspace already holds this plan, paid for this period.",
            )
        return None

    async def _lengthens_term(self, invoice: Invoice, subscription: Subscription) -> bool:
        """Whether this purchase moves the plan the workspace holds to a longer term.

        Monthly to yearly on the same plan is a purchase in its own right
        (ADR-116): the paid month does not make the year a duplicate, and the
        new annual term replaces it at settlement with no credit. The same
        term again is still refused as a duplicate.
        """
        if invoice.billing_interval is None or invoice.interval_count is None:
            return False
        held = await self._catalog.get_price(subscription.plan_price_id)
        if held is None:
            return False
        from app.services.billing_calendar import term_months

        return term_months(invoice.billing_interval, invoice.interval_count) > held.months

    @staticmethod
    def _cancelled_after_opening(invoice: Invoice, subscription: Subscription | None) -> bool:
        """Whether a cancellation was asked for after this invoice was opened."""
        if subscription is None or subscription.cancelled_at is None:
            return False
        if not (
            subscription.cancel_at_period_end or subscription.status is SubscriptionStatus.CANCELLED
        ):
            return False
        opened = invoice.created_at
        return opened is not None and subscription.cancelled_at > opened

    async def _grant(
        self,
        invoice: Invoice,
        *,
        version: PlanVersion,
        price: PlanPrice | None,
        keep_cancellation: bool,
        now: datetime,
    ) -> None:
        """Put the workspace on what it bought, for a term starting now."""
        subscription, previous = await SubscriptionService(
            self._session, tenant_id=self._tenant_id
        ).apply_purchase(version=version, price=price, now=now, keep_cancellation=keep_cancellation)
        # The invoice and the subscription describe the same period: the one
        # this payment opened. Written once, here, at the moment it became
        # true - the checkout knew the interval but not when it would be paid.
        invoice.period_start = subscription.current_period_start
        invoice.period_end = subscription.current_period_end
        invoice.subscription_id = subscription.id
        await self._supersede_unpaid_renewals(subscription, keep=invoice, now=now)
        logger.info(
            "billing.paid_plan_applied",
            extra={
                "event": "billing.paid_plan_applied",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "plan_code": invoice.plan_code,
                "from_status": previous.value if previous is not None else None,
            },
        )

    async def _supersede_unpaid_renewals(
        self,
        subscription: Subscription,
        *,
        keep: Invoice,
        now: datetime,
    ) -> None:
        """Void untouched renewal bills for a period a purchase just replaced.

        An upgrade replaces the current period (spec: upgrades), and the old
        plan's unpaid renewal for it would otherwise go on being chased - and
        could suspend a workspace that has just paid for the new plan. Only a
        renewal with no money on it and no automatic attempt in flight is
        voided; anything else is left for a person.
        """
        rows = await self._session.scalars(
            select(Invoice)
            .where(Invoice.tenant_id == self._tenant_id)
            .where(Invoice.subscription_id == subscription.id)
            .where(Invoice.id != keep.id)
            .where(Invoice.purpose == InvoicePurpose.RENEWAL)
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.amount_paid == 0)
            .where(Invoice.period_end > now)
            .where(
                ~select(Payment.id)
                .where(Payment.invoice_id == Invoice.id)
                .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
                .exists()
            )
        )
        for stale in rows:
            self._move(stale, InvoiceStatus.VOID)
            stale.voided_at = now
            stale.notes = "Superseded by a plan purchase that started a new period."
            self._audit.record(
                AuditAction.INVOICE_VOIDED,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="invoice",
                target_id=stale.id,
                meta={"reason": "superseded_by_purchase", "purchase_invoice_id": str(keep.id)},
            )

    # ------------------------------------------------------------ renewals

    async def renewal_paid(
        self,
        invoice: Invoice,
        *,
        subscription: Subscription | None,
        now: datetime,
    ) -> None:
        """A renewal is paid: lift a payment hold, and adopt its version.

        Restores `PAST_DUE` and `SUSPENDED` and nothing else - a cancellation
        and an expiry are decisions, and paying an old invoice does not undo
        them (ADR-059). Then, if this renewal was issued for a *different*
        version of the current period - a cohort migration, a scheduled
        change to a pricier plan - the subscription moves onto it now that it
        is paid for, and not before (spec: scheduled version migration).
        """
        if subscription is None or subscription.id != invoice.subscription_id:
            return
        if subscription.status in RECOVERABLE_STATUSES:
            previous = subscription.status
            subscription.status = SubscriptionStatus.ACTIVE
            logger.info(
                "billing.subscription_restored",
                extra={
                    "event": "billing.subscription_restored",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "from_status": previous.value,
                },
            )
        await self.adopt_renewal_version(invoice, subscription=subscription, now=now)

    async def adopt_renewal_version(
        self, invoice: Invoice, *, subscription: Subscription, now: datetime
    ) -> None:
        """Move the subscription onto the version and price its current renewal paid for.

        A renewal billed at other terms than the subscription holds - a cohort
        migration, an operator's change to a pricier version or a longer term -
        is adopted only now that it is paid (spec: scheduled version migration).
        The term the renewal paid for is already the subscription's current
        one: the sweep billed it for exactly that window.

        The capacity boundary is judged at `now`, the settlement's own moment,
        like every other boundary: a grace dated by the wall clock instead would
        start - and could end - at a moment the sweep that settled it never saw.
        """
        if (
            invoice.plan_version_id is None
            or (
                invoice.plan_version_id == subscription.plan_version_id
                and invoice.plan_price_id == subscription.plan_price_id
            )
            or invoice.period_start != subscription.current_period_start
            or subscription.is_terminal
        ):
            return
        version = await self._catalog.get_version(invoice.plan_version_id)
        if version is None:
            return
        previous = subscription.plan_version_id
        previous_price = subscription.plan_price_id
        subscription.plan_id = version.plan_id
        subscription.plan_version_id = version.id
        subscription.plan_price_id = invoice.plan_price_id
        if subscription.scheduled_plan_version_id == version.id and (
            subscription.scheduled_plan_price_id == invoice.plan_price_id
        ):
            subscription.clear_scheduled_change()
        self._audit.record(
            AuditAction.SUBSCRIPTION_PLAN_CHANGED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="subscription",
            target_id=subscription.id,
            meta={
                "from_version_id": str(previous) if previous else None,
                "to_version_id": str(version.id),
                "from_price_id": str(previous_price) if previous_price else None,
                "to_price_id": str(invoice.plan_price_id) if invoice.plan_price_id else None,
                "reason": "renewal_settled",
                "invoice_id": str(invoice.id),
                "source": ScheduledChangeSource.MIGRATION.value,
            },
        )
        # Adopting other terms is a capacity boundary (ENT-15): a migration to
        # a smaller version opens the grace, a larger one closes any open.
        await ChannelCapacityReductions(self._session, tenant_id=self._tenant_id).boundary(
            cause=CapacityReductionCause.MIGRATION, now=now
        )

    # ------------------------------------------------------------ void

    async def void(
        self,
        invoice: Invoice,
        *,
        reason: str,
        subscription_policy: str | None,
        now: datetime,
        actor: User | None = None,
    ) -> Invoice:
        """Withdraw an invoice, with explicit consequences (BILL-10, BILL-20).

        Refused for money already on it (refund it first) and for an automatic
        attempt whose outcome is unknown (money may be arriving). A checkout
        voided is a checkout abandoned: nothing is granted. A renewal voided
        while the workspace is behind on it needs a decision about the
        workspace, and none is assumed:

        - ``unchanged`` - the workspace stays as it is. A suspended one can buy
          its way back through a checkout at any time, so this is never a dead
          end.
        - ``cancel`` - the subscription ends now.
        - ``waive`` - the debt is forgiven and service is restored, recorded as
          an invoice waiver with the operator's reason. Never a payment.
        """
        if invoice.status is InvoiceStatus.VOID:
            return invoice
        if invoice.status is InvoiceStatus.PAID or invoice.amount_paid > 0:
            raise ConflictError("An invoice holding money cannot be voided. Refund it instead.")
        unresolved = await self._session.scalar(
            select(Payment.id)
            .where(Payment.invoice_id == invoice.id)
            .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
            .limit(1)
        )
        if unresolved is not None:
            raise ConflictError(
                "An automatic charge for this invoice has no known outcome yet. "
                "Reconcile it before voiding."
            )

        subscription = await self._subscriptions.lock()
        behind = (
            subscription is not None
            and subscription.id == invoice.subscription_id
            and subscription.status in RECOVERABLE_STATUSES
            and invoice.purpose is InvoicePurpose.RENEWAL
        )
        if behind and subscription_policy not in ("unchanged", "cancel", "waive"):
            raise ValidationError(
                "This workspace is behind on this invoice. Say what happens to its "
                "subscription: subscription_policy must be unchanged, cancel or waive."
            )

        self._move(invoice, InvoiceStatus.VOID)
        invoice.voided_at = now
        invoice.notes = reason
        if invoice.purpose is InvoicePurpose.TOPUP:
            await TopupLedger(self._session, tenant_id=self._tenant_id).invoice_voided(
                invoice, now=now
            )

        if behind and subscription is not None:
            if subscription_policy == "cancel":
                subscription.status = SubscriptionStatus.CANCELLED
                subscription.cancelled_at = now
                subscription.cancel_at_period_end = False
                subscription.end_service_at(now)
                subscription.clear_scheduled_change()
                self._audit.record(
                    AuditAction.SUBSCRIPTION_CANCELLED,
                    actor=actor,
                    target_type="subscription",
                    target_id=subscription.id,
                    meta={"immediately": True, "reason": "renewal_voided", "note": reason},
                )
            elif subscription_policy == "waive":
                from app.db.models.billing import BillingAdjustment, BillingAdjustmentKind

                self._session.add(
                    BillingAdjustment(
                        tenant_id=self._tenant_id,
                        subscription_id=subscription.id,
                        kind=BillingAdjustmentKind.INVOICE_WAIVER,
                        plan_version_id=invoice.plan_version_id,
                        invoice_id=invoice.id,
                        reason=reason,
                        actor_id=actor.id if actor is not None else None,
                        starts_at=invoice.period_start,
                        ends_at=invoice.period_end,
                    )
                )
                subscription.status = SubscriptionStatus.ACTIVE
                self._audit.record(
                    AuditAction.SUBSCRIPTION_COMPLIMENTARY_GRANT,
                    actor=actor,
                    target_type="subscription",
                    target_id=subscription.id,
                    meta={
                        "kind": "invoice_waiver",
                        "invoice_id": str(invoice.id),
                        "reason": reason,
                    },
                )
        return invoice

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _move(invoice: Invoice, target: InvoiceStatus) -> None:
        """The only way an invoice changes status here: through the table."""
        if not invoice_may_move(invoice.status, target):
            raise ConflictError(
                f"An invoice cannot go from {invoice.status.value} to {target.value}."
            )
        invoice.status = target


def record_provider_outcome(payment: Payment, event: CallbackEvent, *, now: datetime) -> None:
    """Write what a verified provider event says about one payment attempt.

    The status move itself has already been judged legal by the caller. The
    transaction and the integration it ran on are recorded as provider facts
    for the ledger (BILL-11) - never used to decide anything a payment method
    would change.
    """
    payment.status = event.status
    payment.provider_reference = event.provider_transaction_id
    payment.provider_integration_id = event.integration_id
    payment.failure_reason = event.failure_reason if not event.succeeded else None
    payment.processed_at = now


def money(value: Decimal | int | str) -> Decimal:
    """A value as two-place money."""
    return Decimal(value).quantize(Decimal("0.01"))


__all__ = [
    "APPLIED",
    "DECLINED",
    "DUPLICATE",
    "MISMATCHED",
    "NO_CHANGE",
    "RECOVERABLE_STATUSES",
    "REFUSED",
    "UNMATCHED",
    "InvoiceSettlement",
    "SettlementRows",
]
