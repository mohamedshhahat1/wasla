"""Starting a hosted checkout, and applying the callback that answers it.

Two halves of one flow, kept in one module because they are the two ends of the
same state machine and reading either without the other is misleading.

The rule that shapes everything here: **the browser is never believed.** The
customer chooses a plan code or names one of their own invoices, and nothing
else. The amount, the currency and the workspace are read from the database and
the authenticated session, the reference the provider quotes back is one we
generated, and the payment is only settled by a callback whose signature
checked out. A customer returning to the site with `?success=true` changes
nothing; there is deliberately no endpoint that would let it.

**A checkout is a frozen purchase** (BILL-06). Each one opens its own `CHECKOUT`
invoice naming an immutable plan version, its price, currency and interval, and
that invoice is never re-pointed at another plan. Opening a Business page after
a Pro page therefore leaves the Pro page buying Pro, whichever is paid first;
before, the second checkout re-priced the shared invoice and paying the cheaper
page bought the pricier plan.

**A callback is bound, not merely signed** (BILL-11). The event must name the
Paymob order this system recorded when it created the intention - `order.id`,
which Paymob signs - and must have run on one of this deployment's integrations
in this deployment's mode. `merchant_order_id`, which Paymob does not sign, is
only a cross-check.

**One payment page, several transactions** (BILL-07). A customer whose card is
declined can try again on the same page; that is another transaction on the
same order. A decline is recorded as history and the payment stays pending, so
a later success on the same order settles it.

Settlement itself - what paying an invoice grants - lives in
`app.services.settlement_service`, shared with reconciliation and with manual
payments so the three cannot disagree (BILL-10, BILL-20).

The word "Paymob" appears nowhere below outside comments. This service talks to
a `CheckoutProvider`, which is a protocol in `integrations/billing/checkout.py`
(ADR-031, ADR-044).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WaslaError,
)
from app.core.logging import get_logger
from app.core.telemetry import (
    record_billing_callback,
    record_billing_checkout,
    record_billing_payment,
    record_topup_purchase,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import Plan, PlanVersion, Subscription
from app.db.models.billing_incident import BillingIncidentKind
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    invoice_may_move,
    payment_may_move,
)
from app.db.models.payment_event import MAX_DETAIL_LENGTH, PaymentEvent
from app.db.models.user import User
from app.integrations.billing.checkout import (
    CallbackEvent,
    CheckoutProvider,
    CheckoutRequest,
    EventKind,
)
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.repositories.topup_repository import TopupPurchaseRepository
from app.services import billing_calendar
from app.services.audit_service import AuditTrail
from app.services.billing_incident_service import raise_incident
from app.services.plan_catalog import PlanCatalog
from app.services.settlement_service import (
    APPLIED,
    DECLINED,
    DUPLICATE,
    MISMATCHED,
    NO_CHANGE,
    REFUSED,
    UNMATCHED,
    InvoiceSettlement,
    record_provider_outcome,
)
from app.services.subscription_service import SubscriptionService
from app.services.topup_ledger import TopupLedger

logger = get_logger(__name__)

__all__ = [
    "APPLIED",
    "DECLINED",
    "DUPLICATE",
    "MISMATCHED",
    "NO_CHANGE",
    "REFUSED",
    "UNMATCHED",
    "CheckoutService",
    "StartedCheckout",
]

# How many microseconds a new checkout invoice's provisional period start may
# be nudged to stay distinct from another opened in the same instant.
_PERIOD_NUDGE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class StartedCheckout:
    """Where to send the customer, and what it is for.

    The provider's client secret is deliberately absent. It is a bearer value
    for one payment page: it belongs in the URL the customer follows and
    nowhere else, least of all in a response body a client might log.
    """

    redirect_url: str
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    currency: str


class CheckoutService:
    """Issues hosted checkouts and applies the callbacks that settle them."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: CheckoutProvider | None = None,
        default_plan_code: str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._provider = provider
        # Where a workspace lands when a settlement is reversed (ADR-096).
        # `None` is a real state - a reversal that needs a plan to fall back to
        # and has none logs and leaves the subscription alone.
        self._default_plan_code = default_plan_code
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._catalog = PlanCatalog(session)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session)
        self._settlement = InvoiceSettlement(
            session,
            tenant_id=tenant_id,
            provider_name=provider.name if provider is not None else None,
        )

    @property
    def has_provider(self) -> bool:
        """Whether a payment page can be opened at all in this deployment."""
        return self._provider is not None

    # ------------------------------------------------------------- starting

    async def start(
        self,
        *,
        plan_code: str | None = None,
        invoice_id: uuid.UUID | None = None,
        actor: User | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> StartedCheckout:
        """Open a payment page, either for a plan or for an invoice already due.

        Exactly one of `plan_code` and `invoice_id`. Naming a plan is somebody
        choosing what to buy; naming an invoice is somebody paying a bill this
        system issued them.

        The invoice and the pending payment are written *before* the provider
        is called, so the reference handed to the provider is a row that
        already exists. The provider call is the last thing, and the caller
        commits afterwards.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")
        if (plan_code is None) == (invoice_id is None):
            raise ValidationError("Name either a plan or an invoice, not both.")

        moment = now if now is not None else datetime.now(UTC)
        await self._refuse_repeat(idempotency_key)

        if invoice_id is not None:
            invoice = await self._collectible_invoice(invoice_id)
            description = f"{invoice.plan_code} plan"
        else:
            plan, version = await self._priced_plan(str(plan_code), now=moment)
            subscription = await self._subscriptions.get()
            await self._refuse_purchase(plan, version=version, subscription=subscription)
            invoice = await self._open_invoice(
                plan=plan, version=version, subscription=subscription, now=moment
            )
            description = f"{version.name} plan"

        return await self.open_page(
            invoice, description=description, actor=actor, idempotency_key=idempotency_key
        )

    async def start_offer(
        self,
        *,
        plan: Plan,
        version: PlanVersion,
        offer_id: uuid.UUID,
        actor: User | None,
        idempotency_key: str | None,
        now: datetime,
    ) -> StartedCheckout:
        """Open a payment page for an accepted custom plan offer (ADR-114).

        The caller has locked the offer and checked it may be accepted. The
        invoice is an ordinary `CHECKOUT` pinned to the offered version and
        naming the offer, so settlement grants exactly those terms at exactly
        that price - whatever version the plan has reached by the time the
        money arrives - and can refuse the money if the offer was declined or
        withdrawn in the meantime.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")
        await self._refuse_repeat(idempotency_key)
        subscription = await self._subscriptions.get()
        await self._refuse_purchase(plan, version=version, subscription=subscription)
        invoice = await self._open_invoice(
            plan=plan, version=version, subscription=subscription, now=now, offer_id=offer_id
        )
        return await self.open_page(
            invoice,
            description=f"{version.name} plan",
            actor=actor,
            idempotency_key=idempotency_key,
        )

    async def open_page(
        self,
        invoice: Invoice,
        *,
        description: str,
        actor: User | None,
        idempotency_key: str | None,
    ) -> StartedCheckout:
        """A hosted payment page for one open invoice this workspace owns.

        The one way any customer purchase reaches the provider - a plan, a bill
        already due, or a top-up (ADR-113) - so every page is created, bound
        and settled by the same code. The pending payment is written before
        the provider is called, so the reference handed over is a row that
        already exists; the caller commits afterwards.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")
        await self._session.flush()

        payment = await self._new_attempt(
            invoice,
            provider_name=self._provider.name,
            idempotency_key=idempotency_key,
        )

        session = await self._provider.create_checkout(
            CheckoutRequest(
                # Our id, quoted back by the provider as `merchant_order_id`.
                # Fresh for every attempt, which is why a retried request cannot
                # reuse an earlier page and is refused instead.
                reference=str(payment.id),
                amount=payment.amount,
                currency=payment.currency,
                description=description,
                customer_email=actor.email if actor else None,
                customer_name=actor.full_name if actor else None,
                metadata={"invoice_id": str(invoice.id)},
            )
        )
        payment.provider_intent_reference = session.provider_reference
        payment.provider_order_id = session.order_reference
        payment.provider_mode = session.mode
        invoice.provider = self._provider.name
        await self._session.flush()

        logger.info(
            "billing.checkout_started",
            extra={
                "event": "billing.checkout_started",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "provider": self._provider.name,
                "amount": str(payment.amount),
                "currency": payment.currency,
                # Never the redirect URL: it carries the client secret.
            },
        )
        await record_billing_checkout("created")
        return StartedCheckout(
            redirect_url=session.redirect_url,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=payment.amount,
            currency=payment.currency,
        )

    async def _new_attempt(
        self,
        invoice: Invoice,
        *,
        provider_name: str,
        idempotency_key: str | None,
    ) -> Payment:
        """The pending payment this checkout will collect against.

        The savepoint is for the idempotency key: `_refuse_repeat` reads first
        and produces the good error message, and the unique constraint decides
        two requests that arrive together.
        """
        try:
            async with self._session.begin_nested():
                payment = self._payments.record(
                    invoice_id=invoice.id,
                    status=PaymentStatus.PENDING,
                    amount=invoice.outstanding,
                    currency=invoice.currency,
                    provider=provider_name,
                    provider_reference=None,
                    idempotency_key=idempotency_key,
                )
                await self._session.flush()
        except IntegrityError:
            if not idempotency_key:
                raise
            raise ConflictError(
                "A checkout has already been started for this request. "
                "Read its status rather than starting another."
            ) from None
        return payment

    async def _refuse_repeat(self, idempotency_key: str | None) -> None:
        """Stop a retried request from becoming a second payment page.

        Refused rather than replayed: the response carries a client secret that
        is deliberately never stored (ADR-044), so an honest replay does not
        exist. The unique constraint on `(tenant_id, idempotency_key)` is the
        guarantee; this read only produces the better message.
        """
        if not idempotency_key:
            return
        existing = await self._payments.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            raise ConflictError(
                "A checkout has already been started for this request. "
                "Read its status rather than starting another."
            )

    async def _priced_plan(self, plan_code: str, *, now: datetime) -> tuple[Plan, PlanVersion]:
        """The plan a customer may pay for, and the version they would buy.

        Inactive, private and not-yet-effective plans are refused alike, so the
        refusal confirms nothing about which private codes are real.
        """
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active:
            raise ValidationError("No such plan.")
        if not plan.is_public:
            # A custom plan is never bought by naming its code: its owner
            # accepts the offer made for it, which is what freezes the terms
            # they were shown (ADR-114). Another workspace's custom plan is
            # refused exactly like a private or a missing one, so the refusal
            # confirms nothing about which codes are real.
            if plan.is_custom and plan.tenant_id == self._tenant_id:
                raise ValidationError(
                    "This plan is bought by accepting its offer: "
                    "POST /billing/custom-offers/{id}/accept."
                )
            raise ValidationError("No such plan.")
        version = await self._catalog.current_version(plan, at=now)
        if version is None:
            raise ValidationError("No such plan.")
        if version.price <= 0:
            raise ValidationError("That plan does not require payment.")
        return plan, version

    async def _refuse_purchase(
        self,
        plan: Plan,
        *,
        version: PlanVersion,
        subscription: Subscription | None,
    ) -> None:
        """Refuse, before any money moves, a purchase that cannot be granted.

        Two, and both answer 409 with the way forward:

        - **The plan the workspace is already serving on.** There is nothing to
          buy; a workspace behind on that plan pays the open renewal instead.
        - **A cheaper plan while a pricier paid period runs.** Downgrades take
          effect at the period end so nothing paid for is forfeited, and they
          are scheduled, not bought (spec: downgrades).
        """
        if subscription is None or not subscription.is_serving:
            return
        if subscription.plan_id == plan.id:
            raise ConflictError(
                "This workspace is already on that plan. To settle what it owes, "
                "pay its open invoice instead."
            )
        current = await self._catalog.pinned_version(subscription)
        if current is not None and current.price > 0 and version.price < current.price:
            raise ConflictError(
                "A cheaper plan starts when the current paid period ends. "
                "Schedule it with POST /billing/subscription/plan."
            )

    async def _collectible_invoice(self, invoice_id: uuid.UUID) -> Invoice:
        """One of this workspace's invoices, if a customer may pay it.

        Tenant-scoped, so another workspace's invoice id is indistinguishable
        from one that does not exist. An invoice written off as uncollectible
        is recovered only by an operator (spec: invoice state machine).
        """
        invoice = await self._invoices.get_by_id(invoice_id)
        if invoice is None:
            raise NotFoundError("No such invoice.")
        if invoice.purpose is InvoicePurpose.TOPUP:
            # A top-up is bought through its own checkout, one page per
            # purchase (ADR-113). A second page on the same invoice would be
            # a second chance to pay for one allowance twice.
            raise ConflictError("Top-ups are bought with POST /billing/topups/{id}/checkout.")
        if invoice.status is InvoiceStatus.PAID:
            raise ConflictError("This invoice has already been paid.")
        if invoice.status is not InvoiceStatus.OPEN:
            raise ConflictError("This invoice cannot be collected.")
        if invoice.outstanding <= 0:
            raise ConflictError("Nothing is outstanding on this invoice.")
        return invoice

    async def _open_invoice(
        self,
        *,
        plan: Plan,
        version: PlanVersion,
        subscription: Subscription | None,
        now: datetime,
        offer_id: uuid.UUID | None = None,
    ) -> Invoice:
        """A new, immutable `CHECKOUT` invoice for exactly this version.

        Always new: two checkouts are two independent purchases. The period is
        provisional - one interval from now - and is fixed at settlement, when
        it is known when the paid period actually starts (BILL-03).
        """
        period_start = now
        for _ in range(_PERIOD_NUDGE_ATTEMPTS):
            try:
                async with self._session.begin_nested():
                    created = self._invoices.create(
                        subscription_id=subscription.id if subscription else None,
                        status=InvoiceStatus.OPEN,
                        plan_code=plan.code,
                        amount_due=version.price,
                        currency=version.currency,
                        period_start=period_start,
                        period_end=billing_calendar.add_interval(period_start, version.interval),
                        lines=self._lines(version),
                        purpose=InvoicePurpose.CHECKOUT,
                        plan_version_id=version.id,
                    )
                    created.custom_plan_offer_id = offer_id
                    # When the customer opened this page, in the same clock the
                    # cancellation is written with - settlement compares the two.
                    created.created_at = now
                    await self._session.flush()
                return created
            except IntegrityError:
                period_start = period_start.replace(
                    microsecond=(period_start.microsecond + 1) % 10**6
                )
        raise ConflictError("Another checkout was opened at the same instant. Try again.")

    @staticmethod
    def _lines(version: PlanVersion) -> list[dict[str, object]]:
        """The invoice as it will be read back, with the terms copied in.

        Enough to answer "why was I charged this" without joining anything:
        the plan, its version, the price and what it buys a period of.
        """
        return [
            {
                "kind": "subscription",
                "description": f"{version.name} plan",
                "amount": str(version.price),
                "quantity": 1,
                "plan_version": version.version,
                "interval": version.interval.value,
            }
        ]

    # ------------------------------------------------------------- applying

    async def apply(self, event: CallbackEvent, *, now: datetime | None = None) -> str:
        """Apply one verified callback, exactly once, and say what it did.

        The caller has already authenticated the event; everything here is
        about whether it may be *believed*:

        1. **It must be new.** The `payment_events` insert is the claim.
        2. **It must name a payment we issued**, by the order we recorded.
        3. **That payment must belong to this workspace.**
        4. **It must be bound to us**: our order, our integration, our mode.
        5. **The figures must match what we asked for.**
        6. **The move it asks for must be legal.**
        """
        moment = now if now is not None else datetime.now(UTC)
        payment, retargeted = await self._matching_payment(event)

        record = await self._claim(event, payment=payment, now=moment)
        if record is None:
            await record_billing_callback(DUPLICATE)
            return DUPLICATE

        outcome: str
        detail: str | None
        if retargeted:
            outcome, detail = MISMATCHED, "The unsigned reference disagrees with the signed order."
        else:
            outcome, detail = await self._decide(event, payment=payment, now=moment)
        if outcome == MISMATCHED and payment is not None:
            await raise_incident(
                self._session,
                kind=BillingIncidentKind.MISMATCHED_CALLBACK,
                dedupe_key=event.event_id,
                tenant_id=self._tenant_id,
                payment_id=payment.id,
                invoice_id=payment.invoice_id,
                provider=self._provider_name(),
                provider_transaction_id=event.provider_transaction_id,
                amount=event.amount,
                currency=event.currency or None,
                detail=detail,
                now=moment,
            )
            await self._count_topup_mismatch(payment)
        record.outcome = outcome
        record.detail = detail[:MAX_DETAIL_LENGTH] if detail else None
        record.processed_at = moment
        await self._session.flush()

        logger.info(
            "billing.callback_processed",
            extra={
                "event": "billing.callback_processed",
                "tenant_id": str(self._tenant_id),
                "provider_event_id": event.event_id,
                "event_type": event.event_type,
                "payment_id": str(payment.id) if payment else None,
                "outcome": outcome,
                "detail": detail,
            },
        )
        await record_billing_callback(outcome)
        return outcome

    async def _decide(
        self,
        event: CallbackEvent,
        *,
        payment: Payment | None,
        now: datetime,
    ) -> tuple[str, str | None]:
        """What this callback is allowed to change, and what it changed."""
        if payment is None:
            return UNMATCHED, "No payment matches this reference."

        invoice = await self._invoices.get_by_id(payment.invoice_id)
        if invoice is None or invoice.tenant_id != self._tenant_id:
            return UNMATCHED, "The payment's invoice is not this workspace's."

        problem = self._binding_problem(event, payment=payment)
        if problem is not None:
            logger.warning(
                "billing.callback_unbound",
                extra={
                    "event": "billing.callback_unbound",
                    "payment_id": str(payment.id),
                    "reason": problem,
                },
            )
            return MISMATCHED, problem

        if event.currency.upper() != invoice.currency.upper():
            return MISMATCHED, f"Expected {invoice.currency}, was told {event.currency}."

        if event.kind in (EventKind.REFUNDED, EventKind.VOIDED):
            return await self._apply_reversal(event, payment=payment, invoice=invoice, now=now)
        return await self._apply_collection(event, payment=payment, invoice=invoice, now=now)

    def _binding_problem(self, event: CallbackEvent, *, payment: Payment) -> str | None:
        """Why this event cannot be about this payment (BILL-11), or None.

        The order comes first. A payment created before orders were recorded
        (migration 0071) has none to compare with; a *collection* on one is
        refused - the fail-safe answer for a stale page, which an operator can
        reconcile - while a *reversal* of money it already holds is still
        applied, because refusing to record a refund would leave the ledger
        claiming money the customer has been given back.
        """
        reversal = event.kind in (EventKind.REFUNDED, EventKind.VOIDED)
        if payment.provider_order_id is None:
            if not reversal:
                return "This payment predates order binding; reconcile it instead."
        elif event.order_id != payment.provider_order_id:
            return (
                f"The callback is for order {event.order_id}, "
                f"this payment's order is {payment.provider_order_id}."
            )

        # A provider that can bind callbacks says so by implementing
        # `CallbackBindingProvider`; Paymob does. One that cannot (a test
        # double) is bound by the order check above alone.
        check = getattr(self._provider, "callback_binding_problem", None)
        if check is None:
            return None
        problem: str | None = check(event, automatic=payment.is_automatic)
        return problem

    async def _apply_collection(
        self,
        event: CallbackEvent,
        *,
        payment: Payment,
        invoice: Invoice,
        now: datetime,
    ) -> tuple[str, str | None]:
        """A callback reporting what happened to an attempt at collecting."""
        if event.amount != payment.amount:
            logger.warning(
                "billing.callback_amount_mismatch",
                extra={
                    "event": "billing.callback_amount_mismatch",
                    "payment_id": str(payment.id),
                    "expected_amount": str(payment.amount),
                    "reported_amount": str(event.amount),
                },
            )
            payment.failure_reason = "The provider reported a different amount."
            await record_billing_payment("mismatched")
            return MISMATCHED, f"Expected {payment.amount}, was told {event.amount}."

        if (
            event.status is PaymentStatus.FAILED
            and not payment.is_automatic
            and payment.status is PaymentStatus.PENDING
        ):
            # A declined transaction on a hosted page (BILL-07). The customer can
            # try again on the same page - another transaction on the same order
            # - so the logical payment stays pending and the decline is history:
            # the event row keeps it, and `failure_reason` tells the customer.
            payment.failure_reason = event.failure_reason or "The card was declined."
            await record_billing_payment("declined")
            await record_billing_checkout("failed")
            return DECLINED, "Transaction declined; the payment page can still be paid."

        if event.status is payment.status:
            if (
                payment.status is PaymentStatus.SUCCEEDED
                and event.provider_transaction_id
                and payment.provider_reference
                and event.provider_transaction_id != payment.provider_reference
            ):
                # A *second* successful transaction on an order that was already
                # paid: the customer was charged twice for one page (BILL-15).
                await raise_incident(
                    self._session,
                    kind=(
                        BillingIncidentKind.TOPUP_DUPLICATE_PAYMENT
                        if invoice.purpose is InvoicePurpose.TOPUP
                        else BillingIncidentKind.DUPLICATE_PAYMENT
                    ),
                    dedupe_key=f"{payment.id}:{event.provider_transaction_id}",
                    tenant_id=self._tenant_id,
                    payment_id=payment.id,
                    invoice_id=invoice.id,
                    provider=self._provider_name(),
                    provider_transaction_id=event.provider_transaction_id,
                    amount=event.amount,
                    currency=event.currency,
                    detail="A second successful transaction on an order already paid.",
                    now=now,
                )
                await record_billing_payment("duplicate")
                return REFUSED, "A second success on an order that was already paid."
            return NO_CHANGE, f"Already {payment.status.value}."
        if not payment_may_move(payment.status, event.status):
            logger.warning(
                "billing.callback_illegal_transition",
                extra={
                    "event": "billing.callback_illegal_transition",
                    "payment_id": str(payment.id),
                    "from_status": payment.status.value,
                    "to_status": event.status.value,
                },
            )
            return REFUSED, f"{payment.status.value} cannot become {event.status.value}."

        record_provider_outcome(payment, event, now=now)
        if payment.is_unresolved_collection:
            # An automatic attempt has just learned its outcome, so the invoice
            # behind it stops being blocked. Written here because *this* is
            # where the answer arrives - the charge request only ever asked
            # (ADR-088).
            payment.collection_state = CollectionState.SETTLED

        if not event.succeeded:
            await record_billing_payment("failed")
            return APPLIED, f"Payment {event.status.value}."
        await record_billing_payment("succeeded")
        outcome, detail = await self._settlement.settle(invoice, payment=payment, now=now)
        if outcome == APPLIED and invoice.purpose in (
            InvoicePurpose.CHECKOUT,
            InvoicePurpose.TOPUP,
        ):
            await record_billing_checkout("settled")
        elif outcome == REFUSED:
            await record_billing_payment("refused")
        return outcome, detail

    async def _apply_reversal(
        self,
        event: CallbackEvent,
        *,
        payment: Payment,
        invoice: Invoice,
        now: datetime,
    ) -> tuple[str, str | None]:
        """A callback reporting that money we collected has gone back.

        Arrives whether or not this system asked for it: a refund issued from
        the provider's dashboard and one an operator approved here produce the
        same notification, and both must land in the same place.

        Three shapes, and the difference is who decided (BILL-13):

        - **An operator's partial refund** is a goodwill credit. The invoice
          stays paid, the customer keeps the period, and nobody is billed for
          the difference.
        - **A full refund**, however it was asked for, empties the invoice; the
          plan it bought is withdrawn if nothing else pays for it (ADR-096),
          and an operator's full refund voids the invoice as well, so no sweep
          ever tries to collect money that was deliberately given back.
        - **An unrequested partial reversal** - a chargeback, a dashboard
          refund - leaves a genuine debt: the invoice reopens and is dunned.
        """
        refunded = event.refunded_amount if event.refunded_amount else event.amount
        if refunded <= 0 or refunded > payment.amount:
            return MISMATCHED, f"Refund of {refunded} against a payment of {payment.amount}."
        if payment.status not in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED):
            return REFUSED, f"{payment.status.value} was never collected."
        if refunded <= payment.refunded_amount:
            return NO_CHANGE, f"Already refunded {payment.refunded_amount}."

        requested = payment.refund_requested_amount
        operator_requested = requested is not None and refunded <= requested
        returned = refunded - payment.refunded_amount
        payment.refunded_amount = refunded
        payment.refunded_at = now
        if requested is not None and refunded >= requested:
            # The standing request is fulfilled. Cleared so the next request -
            # for what is left - is not refused as a duplicate of this one.
            payment.refund_requested_amount = None
        if refunded >= payment.amount and payment_may_move(payment.status, PaymentStatus.REFUNDED):
            payment.status = PaymentStatus.REFUNDED

        invoice.amount_paid = max(invoice.amount_paid - returned, Decimal("0.00"))
        full = invoice.amount_paid <= 0
        goodwill = operator_requested and not full
        if (
            invoice.status is InvoiceStatus.PAID
            and invoice.amount_paid < invoice.amount_due
            and not goodwill
            and invoice_may_move(invoice.status, InvoiceStatus.OPEN)
        ):
            invoice.status = InvoiceStatus.OPEN
            invoice.paid_at = None

        topup = invoice.purpose is InvoicePurpose.TOPUP
        if invoice.status is InvoiceStatus.OPEN:
            if full:
                await self._withdraw_purchased_plan(invoice, now=now)
                if operator_requested and invoice_may_move(invoice.status, InvoiceStatus.VOID):
                    invoice.status = InvoiceStatus.VOID
                    invoice.voided_at = now
                    invoice.notes = "Refunded in full at the platform's decision."
            elif invoice.issued_at is None and not topup:
                # An unrequested part-reversal is the one shape in which a
                # checkout row becomes a genuine debt, so the dunning clock
                # starts here (ADR-096). Never for a top-up: dunning one could
                # suspend a workspace over an add-on, and an operator decides
                # instead (ADR-113).
                invoice.issued_at = now
        if topup:
            await TopupLedger(self._session, tenant_id=self._tenant_id).invoice_reversed(
                invoice,
                payment=payment,
                full=full,
                operator_requested=operator_requested,
                now=now,
            )

        self._audit.record(
            AuditAction.PAYMENT_REFUNDED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            tenant_id=self._tenant_id,
            target_type="payment",
            target_id=payment.id,
            meta={
                "amount": str(returned),
                "refunded_total": str(refunded),
                "currency": invoice.currency,
                "kind": event.kind.value,
                "operator_requested": operator_requested,
                "provider_reference": event.provider_transaction_id,
            },
        )
        logger.info(
            "billing.refund_applied",
            extra={
                "event": "billing.refund_applied",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "amount": str(returned),
            },
        )
        from app.core.telemetry import record_billing_refund

        await record_billing_refund("confirmed")
        return APPLIED, f"Refunded {returned}."

    def _provider_name(self) -> str:
        return self._provider.name if self._provider is not None else "unknown"

    async def _count_topup_mismatch(self, payment: Payment) -> None:
        """Count a refused callback against a top-up's entitlement, for its alert."""
        invoice = await self._invoices.get_by_id(payment.invoice_id)
        if invoice is None or invoice.purpose is not InvoicePurpose.TOPUP:
            return
        purchase = await TopupPurchaseRepository(
            self._session, tenant_id=self._tenant_id
        ).lock_for_invoice(invoice.id)
        if purchase is not None:
            await record_topup_purchase(purchase.entitlement_key.value, "callback_mismatched")

    async def _matching_payment(self, event: CallbackEvent) -> tuple[Payment | None, bool]:
        """The payment this callback names, and whether it was re-aimed.

        By the provider's **order** first - the signed identifier, recorded
        when this system created the intention (BILL-11). The unsigned
        `merchant_order_id` must then agree with it; a callback whose signed
        order is ours but whose reference names a different payment has been
        edited, and is reported as re-aimed rather than applied to either.

        A payment created before orders were recorded is found by our own
        reference, and the binding check refuses a collection on it. A
        reversal is also findable by the transaction id recorded when the money
        arrived, because a refund names the transaction it reverses.
        """
        provider = self._provider_name()
        if event.order_id:
            found = await self._payments.get_by_order(provider=provider, order_id=event.order_id)
            if found is not None:
                retargeted = bool(event.reference) and event.reference != str(found.id)
                return found, retargeted

        if event.reference:
            try:
                payment_id = uuid.UUID(event.reference)
            except ValueError:
                payment_id = None
            if payment_id is not None:
                found = await self._payments.get_by_id(payment_id)
                if found is not None:
                    return found, False

        for transaction_id in (event.parent_transaction_id, event.provider_transaction_id):
            if not transaction_id:
                continue
            found = await self._payments.get_by_transaction(
                provider=provider,
                transaction_id=transaction_id,
            )
            if found is not None:
                return found, False
        return None, False

    async def _claim(
        self,
        event: CallbackEvent,
        *,
        payment: Payment | None,
        now: datetime,
    ) -> PaymentEvent | None:
        """Take ownership of this event, or report that somebody already has.

        A savepoint, because a unique violation poisons the transaction it
        happens in and this one has an invoice to settle afterwards.
        """
        record = PaymentEvent(
            provider=self._provider_name(),
            provider_event_id=event.event_id,
            provider_transaction_id=event.provider_transaction_id,
            event_type=event.event_type,
            payment_id=payment.id if payment is not None else None,
            outcome=NO_CHANGE,
            received_at=now,
            processed_at=None,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError:
            logger.info(
                "billing.callback_duplicate",
                extra={
                    "event": "billing.callback_duplicate",
                    "provider_event_id": event.event_id,
                },
            )
            return None
        return record

    async def _withdraw_purchased_plan(self, invoice: Invoice, *, now: datetime) -> None:
        """Take back the plan an invoice bought, now that nothing paid for it.

        The mirror of settlement's grant, reached only when every unit
        collected against the invoice has gone back (ADR-096). A partial
        reversal leaves the plan alone. Four conditions:

        - there is a subscription, and a plan to fall back to;
        - it is not terminal;
        - the workspace is still on the plan this invoice bought;
        - nothing else covers the plan right now.

        Failure is contained: the record that a customer was repaid is the
        part that must never be rolled back.
        """
        if invoice.purpose is InvoicePurpose.TOPUP:
            # A top-up bought no plan, so refunding one withdraws no plan
            # (ADR-113); `TopupLedger.invoice_reversed` decides what happens to
            # the allowance.
            return
        subscription = await self._subscriptions.get()
        if subscription is None or subscription.is_terminal:
            return
        if not self._default_plan_code:
            logger.warning(
                "billing.reversal_without_default_plan",
                extra={
                    "event": "billing.reversal_without_default_plan",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "plan_code": invoice.plan_code,
                },
            )
            return
        if invoice.plan_code == self._default_plan_code:
            return

        current = await self._plans.get_by_id(subscription.plan_id)
        if current is None or current.code != invoice.plan_code:
            return
        if await self._invoices.has_other_settled_cover(
            invoice_id=invoice.id,
            plan_code=invoice.plan_code,
            at=now,
        ):
            logger.info(
                "billing.reversal_kept_plan",
                extra={
                    "event": "billing.reversal_kept_plan",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "plan_code": invoice.plan_code,
                },
            )
            return

        try:
            await SubscriptionService(self._session, tenant_id=self._tenant_id).change_plan(
                plan_code=self._default_plan_code,
                now=now,
                self_service=False,
                actor=None,
            )
        except WaslaError:
            logger.exception(
                "billing.plan_withdrawal_failed",
                extra={
                    "event": "billing.plan_withdrawal_failed",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "plan_code": invoice.plan_code,
                },
            )
            return

        self._audit.record(
            AuditAction.SUBSCRIPTION_PLAN_WITHDRAWN,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            tenant_id=self._tenant_id,
            target_type="subscription",
            target_id=subscription.id,
            target_label=self._default_plan_code,
            meta={
                "invoice_id": str(invoice.id),
                "plan_code": invoice.plan_code,
                "reason": "settlement_reversed",
            },
        )
        logger.info(
            "billing.plan_withdrawn",
            extra={
                "event": "billing.plan_withdrawn",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "from_plan": invoice.plan_code,
                "to_plan": self._default_plan_code,
            },
        )

    async def require_payment(self, payment_id: uuid.UUID) -> Payment:
        """One payment of this workspace's, or a 404."""
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundError("No such payment.")
        return payment
