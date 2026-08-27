"""Starting a hosted checkout, and applying the callback that answers it.

Two halves of one flow, kept in one module because they are the two ends of the
same state machine and reading either without the other is misleading.

The rule that shapes everything here: **the browser is never believed.** The
customer chooses a plan code and nothing else. The amount, the currency and the
workspace are read from the database and the authenticated session, the
reference the provider quotes back is one we generated, and the payment is only
settled by a callback whose signature checked out. A customer returning to the
site with `?success=true` changes nothing; there is deliberately no endpoint
that would let it.

The word "Paymob" appears nowhere below. This service talks to a
`CheckoutProvider`, which is a protocol in `integrations/billing/checkout.py`,
and the day a second processor is added it is constructed instead (ADR-031,
ADR-044).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import Plan, Subscription
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.user import User
from app.integrations.billing.checkout import (
    CallbackEvent,
    CheckoutProvider,
    CheckoutRequest,
)
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.services.audit_service import AuditTrail
from app.services.subscription_service import add_interval

logger = get_logger(__name__)

# What a recorded callback did, in one word. Read by filtering, so a closed
# vocabulary rather than a message.
APPLIED: Final = "applied"
DUPLICATE: Final = "duplicate"
UNMATCHED: Final = "unmatched"
MISMATCHED: Final = "mismatched"


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
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._provider = provider
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session)

    # ------------------------------------------------------------- starting

    async def start(
        self,
        *,
        plan_code: str,
        actor: User | None = None,
        now: datetime | None = None,
    ) -> StartedCheckout:
        """Price a plan from the database and open a checkout for it.

        The order matters. The invoice and the pending payment are written
        *before* the provider is called, so the reference handed to the
        provider is a row that already exists: a callback can never arrive for
        a payment this system has not heard of because the customer was fast.

        The provider call is the last thing, and it is outside no transaction -
        the caller commits afterwards. A provider that succeeds and a commit
        that then fails leaves an intention nobody will pay against, which
        costs nothing; the reverse ordering would leave a customer at a payment
        page for an invoice that does not exist.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")

        moment = now if now is not None else datetime.now(UTC)
        plan = await self._priced_plan(plan_code)
        subscription = await self._subscriptions.get()

        invoice = await self._open_invoice(plan=plan, subscription=subscription, now=moment)
        payment = self._payments.record(
            invoice_id=invoice.id,
            status=PaymentStatus.PENDING,
            amount=invoice.outstanding,
            currency=invoice.currency,
            provider=self._provider.name,
            # No reference yet. It is the *transaction* id, which does not
            # exist until somebody actually pays; the unique constraint on
            # (provider, provider_reference) treats NULLs as distinct, so
            # several abandoned attempts can coexist.
            provider_reference=None,
        )
        await self._session.flush()

        session = await self._provider.create_checkout(
            CheckoutRequest(
                # Our id, quoted back by the provider, and the whole mapping
                # from a callback to this row.
                reference=str(payment.id),
                amount=invoice.outstanding,
                currency=invoice.currency,
                description=f"{plan.name} plan",
                customer_email=actor.email if actor else None,
                customer_name=actor.full_name if actor else None,
                # Correlation only, and nothing that would matter if disclosed:
                # this travels to a third party and comes back through a
                # request anybody can send at our webhook.
                metadata={"invoice_id": str(invoice.id)},
            )
        )
        payment.provider_intent_reference = session.provider_reference
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
                "plan_code": plan.code,
                # Never the redirect URL: it carries the client secret.
            },
        )
        return StartedCheckout(
            redirect_url=session.redirect_url,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=invoice.outstanding,
            currency=invoice.currency,
        )

    async def _priced_plan(self, plan_code: str) -> Plan:
        """The plan a customer may pay for, priced by us.

        `is_public` is enforced here as it is in `SubscriptionService`: a
        checkout is another door onto plan selection, and a door that skipped
        the check would let somebody pay the Enterprise price - or, worse, the
        Enterprise *limits* at whatever price that row happens to carry.
        """
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active or not plan.is_public:
            # The same refusal for all three, as elsewhere: distinguishing them
            # confirms which private codes are real.
            raise ValidationError("No such plan.")
        if plan.price <= 0:
            # A free plan has nothing to collect. Sending somebody to a payment
            # page for zero is a confusing dead end, and providers refuse it.
            raise ValidationError("That plan does not require payment.")
        return plan

    async def _open_invoice(
        self,
        *,
        plan: Plan,
        subscription: Subscription | None,
        now: datetime,
    ) -> Invoice:
        """The invoice this checkout collects, reusing one if it is already open.

        Reuse rather than issue-per-attempt, and the constraint decides it
        either way: `UNIQUE(tenant_id, period_start)` means a second attempt at
        the same period cannot create a second invoice. Somebody who abandons a
        checkout and starts another gets a second *payment* against one
        invoice, which is exactly what the payments table is for - attempts are
        rows, and the history is what a dispute turns on.
        """
        period_start = subscription.current_period_start if subscription else now
        period_end = (
            subscription.current_period_end if subscription else add_interval(now, plan.interval)
        )

        existing = await self._invoices.get_for_period(period_start=period_start)
        if existing is not None:
            if existing.status is InvoiceStatus.PAID:
                raise ConflictError("This period has already been paid.")
            if existing.is_terminal:
                raise ConflictError("This invoice is settled and cannot be collected.")
            return existing

        return self._invoices.create(
            subscription_id=subscription.id if subscription else None,
            status=InvoiceStatus.OPEN,
            plan_code=plan.code,
            amount_due=plan.price,
            currency=plan.currency,
            period_start=period_start,
            period_end=period_end,
            lines=[
                {
                    "description": f"{plan.name} plan",
                    "amount": str(plan.price),
                    "quantity": 1,
                }
            ],
        )

    # ------------------------------------------------------------- applying

    async def apply(self, event: CallbackEvent, *, now: datetime | None = None) -> str:
        """Apply one verified callback, exactly once, and say what it did.

        The caller has already authenticated the event; everything here is
        about whether it may be *believed*, which is a different question. Four
        refusals stand between a verified callback and a settled invoice:

        1. **It must be new.** The `payment_events` insert is the claim, and
           the unique constraint decides races rather than a preceding read.
        2. **It must name a payment we issued**, by a reference we generated.
        3. **That payment must belong to this workspace.** A callback cannot
           reach across a tenant boundary even if a reference leaked.
        4. **The amount and currency must match what we asked for.** A provider
           reporting a different figure is not settling this invoice, whatever
           it says.

        Returns the outcome word, which the endpoint turns into a response that
        is the same for all of them.
        """
        moment = now if now is not None else datetime.now(UTC)
        payment = await self._matching_payment(event)
        outcome = await self._claim(event, payment=payment, now=moment)
        if outcome is not None:
            return outcome

        if payment is None:
            logger.warning(
                "billing.callback_unmatched",
                extra={
                    "event": "billing.callback_unmatched",
                    "provider": self._provider_name(),
                    "provider_event_id": event.event_id,
                },
            )
            return UNMATCHED

        invoice = await self._invoices.get_by_id(payment.invoice_id)
        if invoice is None or invoice.tenant_id != self._tenant_id:
            return UNMATCHED

        if event.currency.upper() != invoice.currency.upper() or event.amount != payment.amount:
            # Recorded rather than applied. A provider that says it collected a
            # different amount than we asked for has done something we do not
            # understand, and settling the invoice anyway would paper over it.
            logger.warning(
                "billing.callback_amount_mismatch",
                extra={
                    "event": "billing.callback_amount_mismatch",
                    "payment_id": str(payment.id),
                    "expected_amount": str(payment.amount),
                    "reported_amount": str(event.amount),
                    "expected_currency": invoice.currency,
                    "reported_currency": event.currency,
                },
            )
            payment.failure_reason = "The provider reported a different amount."
            return MISMATCHED

        self._apply_to_payment(payment, event=event, now=moment)
        if event.succeeded:
            await self._settle(invoice, payment=payment, now=moment)

        await self._session.flush()
        logger.info(
            "billing.callback_applied",
            extra={
                "event": "billing.callback_applied",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "status": payment.status.value,
            },
        )
        return APPLIED

    def _provider_name(self) -> str:
        return self._provider.name if self._provider is not None else "unknown"

    async def _matching_payment(self, event: CallbackEvent) -> Payment | None:
        """The payment this callback names, if it is ours.

        By our own reference, never by anything the provider chose. The tenant
        filter on the repository is what stops a callback naming another
        workspace's payment from being applied to this one.
        """
        if not event.reference:
            return None
        try:
            payment_id = uuid.UUID(event.reference)
        except ValueError:
            return None
        return await self._payments.get_by_id(payment_id)

    async def _claim(
        self,
        event: CallbackEvent,
        *,
        payment: Payment | None,
        now: datetime,
    ) -> str | None:
        """Take ownership of this event, or report that somebody already has.

        A savepoint, because a unique violation poisons the transaction it
        happens in and this one has an invoice to settle afterwards. The nested
        block is released on success and rolled back on the collision, leaving
        the outer transaction usable either way.
        """
        record = PaymentEvent(
            provider=self._provider_name(),
            provider_event_id=event.event_id,
            payment_id=payment.id if payment is not None else None,
            outcome=APPLIED,
            processed_at=now,
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
            return DUPLICATE
        return None

    @staticmethod
    def _apply_to_payment(payment: Payment, *, event: CallbackEvent, now: datetime) -> None:
        payment.status = event.status
        payment.provider_reference = event.provider_payment_id
        payment.failure_reason = event.failure_reason
        payment.processed_at = now

    async def _settle(self, invoice: Invoice, *, payment: Payment, now: datetime) -> None:
        """Money arrived: mark the invoice paid and put the subscription right.

        Deliberately does not *create* a subscription. Paying an invoice
        settles an invoice; which plan a workspace is on is
        `SubscriptionService`'s decision and has its own rules about trials and
        periods. What this does is the narrow thing a payment means: a
        workspace that was behind is no longer behind.
        """
        invoice.amount_paid = invoice.amount_paid + payment.amount
        invoice.provider_reference = payment.provider_reference
        if invoice.amount_paid >= invoice.amount_due:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = now

        subscription = await self._subscriptions.get()
        if subscription is not None and subscription.id == invoice.subscription_id:
            from app.db.models.billing import SubscriptionStatus

            if subscription.status is SubscriptionStatus.PAST_DUE:
                # The one state a payment changes on its own. A trial stays a
                # trial and a cancellation stays cancelled: paying an invoice
                # is not a request to resubscribe, and treating it as one would
                # revive a subscription somebody deliberately ended.
                subscription.status = SubscriptionStatus.ACTIVE

        self._audit.record(
            AuditAction.PAYMENT_RECORDED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            tenant_id=self._tenant_id,
            target_type="invoice",
            target_id=invoice.id,
            meta={
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": invoice.currency,
                "provider": payment.provider,
            },
        )

    async def require_payment(self, payment_id: uuid.UUID) -> Payment:
        """One payment of this workspace's, or a 404.

        Tenant-scoped through the repository, so another workspace's payment id
        is indistinguishable from one that does not exist.
        """
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundError("No such payment.")
        return payment
