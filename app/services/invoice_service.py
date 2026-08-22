"""Turning a finished period into an invoice, and recording what was paid.

The shape of an invoice here is deliberately simple: one line for the plan's
subscription fee, plus a record of what the workspace consumed during the period.
The usage lines carry **no amount** today, and that absence is the honest part —
per-unit overage pricing is not stored anywhere, and a figure invented from
nothing would be a number a customer is asked to pay.

So an invoice says: this is the plan you were on, this is what it costs, and
this is what you used. When overage pricing exists, the usage lines gain amounts
and nothing else about this file changes.

Three rules hold the whole thing together:

**An issued invoice never changes.** Amounts are copied at issue time, not
joined for. A plan repriced in April cannot alter March.

**One invoice per workspace per period**, enforced by a unique constraint rather
than by a check here, because two replicas sweeping at once is exactly when a
check in Python fails.

**A payment attempt is a row.** Failures are kept, because the history is what a
dispute turns on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.db.models.billing import Plan, Subscription
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.usage import UsageEventType
from app.integrations.billing.base import PaymentProvider
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.repositories.usage_repository import UsageEventRepository

logger = get_logger(__name__)

# The meters an invoice reports. Not every meter: a customer reading a bill
# wants the figures they recognise from their own dashboard, not thirteen rows
# including one counting bytes.
BILLED_METERS: tuple[UsageEventType, ...] = (
    UsageEventType.WHATSAPP_MESSAGE_SENT,
    UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
    UsageEventType.AI_REQUEST,
    UsageEventType.CAMPAIGN_MESSAGE,
)


class InvoiceService:
    """Invoicing for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: PaymentProvider | None = None,
    ) -> None:
        """`provider` is needed only to collect.

        Issuing an invoice and reading one back are database work. A request
        that lists invoices constructs this without a provider, exactly as a
        campaign service is built without a messaging client.
        """
        self._session = session
        self._tenant_id = tenant_id
        self._provider = provider
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._usage = UsageEventRepository(session, tenant_id=tenant_id)

    async def issue_for_period(
        self,
        *,
        subscription: Subscription,
        plan: Plan,
        period_start: datetime,
        period_end: datetime,
        now: datetime | None = None,
    ) -> tuple[Invoice, bool]:
        """Bill a finished period. Returns the invoice and whether it is new.

        Idempotent by period: a sweep that runs twice finds the invoice it
        already issued and returns it unchanged. That is what makes the sweep
        safe to retry, and it is checked here so a second run is a no-op rather
        than an integrity error.
        """
        moment = now if now is not None else datetime.now(UTC)
        existing = await self._invoices.get_for_period(period_start=period_start)
        if existing is not None:
            return existing, False

        lines = await self._lines(
            plan=plan,
            period_start=period_start,
            period_end=period_end,
        )
        invoice = self._invoices.create(
            subscription_id=subscription.id,
            plan_code=plan.code,
            amount_due=plan.price,
            currency=plan.currency,
            period_start=period_start,
            period_end=period_end,
            lines=lines,
            status=InvoiceStatus.OPEN if plan.price > 0 else InvoiceStatus.PAID,
        )
        invoice.issued_at = moment
        if invoice.status is InvoiceStatus.PAID:
            # A free plan produces an invoice that is settled on arrival. It is
            # still issued, because "you were on Starter and used this much" is
            # worth a record even when the amount is zero.
            invoice.paid_at = moment
        await self._session.flush()

        logger.info(
            "billing.invoice_issued",
            extra={
                "event": "billing.invoice_issued",
                "tenant_id": str(self._tenant_id),
                "plan": plan.code,
                "amount_due": str(invoice.amount_due),
            },
        )
        return invoice, True

    async def _lines(
        self,
        *,
        plan: Plan,
        period_start: datetime,
        period_end: datetime,
    ) -> list[dict[str, Any]]:
        """The subscription line, then what was used.

        Usage lines carry a quantity and no amount. Nothing stores a per-unit
        price, and inventing one would put a number on a bill that no pricing
        decision stands behind.
        """
        lines: list[dict[str, Any]] = [
            {
                "kind": "subscription",
                "description": f"{plan.name} plan",
                "quantity": 1,
                "amount": str(plan.price),
            }
        ]

        totals = await self._usage.totals(
            since=period_start,
            until=period_end,
            event_types=BILLED_METERS,
        )
        lines.extend(
            {
                "kind": "usage",
                "description": total.event_type.value,
                "quantity": total.quantity,
                "unit": total.unit.value,
                # Included at zero rather than omitted, so the shape of a line
                # never depends on whether overage pricing exists yet.
                "amount": "0.00",
            }
            for total in sorted(totals, key=lambda item: item.event_type.value)
        )
        return lines

    async def collect(self, invoice: Invoice, *, now: datetime | None = None) -> Payment:
        """Ask the provider for the money, and record what it said.

        A decline is recorded and returned, not raised: it is an answer, and the
        invoice stays open so it can be tried again. Only an unreachable
        provider raises, because that is our problem rather than the customer's.
        """
        moment = now if now is not None else datetime.now(UTC)
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")
        if invoice.is_terminal:
            raise ConflictError("This invoice is settled and cannot be collected again.")

        # Stable for the invoice, so a retried request collects once.
        key = f"invoice:{invoice.id}"
        outcome = await self._provider.charge(
            amount=invoice.outstanding,
            currency=invoice.currency,
            idempotency_key=key,
            description=f"{invoice.plan_code} plan",
        )

        existing = await self._payments.get_by_reference(
            provider=self._provider.name,
            reference=outcome.reference or key,
        )
        if existing is not None:
            # The provider recognised its own idempotency key, so this attempt
            # is the one already recorded rather than a second charge.
            return existing

        payment = self._payments.record(
            invoice_id=invoice.id,
            status=outcome.status,
            amount=outcome.amount,
            currency=invoice.currency,
            provider=self._provider.name,
            provider_reference=outcome.reference or key,
            failure_reason=outcome.failure_reason,
            processed_at=moment if outcome.status is not PaymentStatus.PENDING else None,
        )
        invoice.provider = self._provider.name
        invoice.provider_reference = outcome.reference

        if outcome.succeeded:
            self._settle(invoice, amount=outcome.amount, now=moment)
        await self._session.flush()
        return payment

    async def record_payment(
        self,
        *,
        invoice_id: uuid.UUID,
        amount: Decimal,
        provider: str,
        reference: str | None = None,
        now: datetime | None = None,
    ) -> Payment:
        """Record money that arrived outside the system.

        A bank transfer, a card taken over the phone. This is how the manual
        provider is actually settled, and it exists as its own operation because
        somebody has to have *seen* the money — a provider that marked its own
        pending invoices paid would be inventing collections.
        """
        moment = now if now is not None else datetime.now(UTC)
        invoice = await self._invoices.require_by_id(invoice_id)
        if invoice.status is InvoiceStatus.VOID:
            raise ConflictError("A voided invoice cannot be paid.")
        if amount <= 0:
            raise ValidationError("A payment must be for a positive amount.")

        payment = self._payments.record(
            invoice_id=invoice.id,
            status=PaymentStatus.SUCCEEDED,
            amount=amount,
            currency=invoice.currency,
            provider=provider,
            provider_reference=reference,
            processed_at=moment,
        )
        self._settle(invoice, amount=amount, now=moment)
        await self._session.flush()
        logger.info(
            "billing.payment_recorded",
            extra={
                "event": "billing.payment_recorded",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "amount": str(amount),
            },
        )
        return payment

    def _settle(self, invoice: Invoice, *, amount: Decimal, now: datetime) -> None:
        """Apply money to an invoice, marking it paid once it is covered.

        Part payments are kept as part payments: the invoice stays open with a
        smaller outstanding balance, because a customer who paid half has paid
        half.
        """
        invoice.amount_paid = invoice.amount_paid + amount
        if invoice.amount_paid >= invoice.amount_due:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = now

    async def void(self, invoice_id: uuid.UUID, *, reason: str | None = None) -> Invoice:
        """Withdraw an invoice that should not have been issued.

        Voided rather than deleted or edited: the customer has seen it, and a
        bill that silently changes is worse than one that is visibly withdrawn.
        A paid invoice cannot be voided - that is a refund, which is a different
        operation and a different conversation.
        """
        invoice = await self._invoices.require_by_id(invoice_id)
        if invoice.status is InvoiceStatus.PAID:
            raise ConflictError("A paid invoice cannot be voided. Refund it instead.")
        if invoice.status is InvoiceStatus.VOID:
            return invoice

        invoice.status = InvoiceStatus.VOID
        invoice.voided_at = datetime.now(UTC)
        if reason:
            invoice.notes = reason
        return invoice

    async def list_invoices(self, *, limit: int = 50) -> list[Invoice]:
        return await self._invoices.list_invoices(limit=limit)

    async def get(self, invoice_id: uuid.UUID) -> Invoice:
        return await self._invoices.require_by_id(invoice_id)

    async def payments_for(self, invoice_id: uuid.UUID) -> list[Payment]:
        await self._invoices.require_by_id(invoice_id)
        return await self._payments.list_for_invoice(invoice_id)
