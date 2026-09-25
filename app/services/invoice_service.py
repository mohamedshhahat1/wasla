"""Billing a service period in advance, and recording what was paid.

The shape of an invoice here is deliberately simple: one line for the plan's
subscription fee, plus a record of what the workspace consumed during the period.
The usage lines carry **no amount** today, and that absence is the honest part —
per-unit overage pricing is not stored anywhere, and a figure invented from
nothing would be a number a customer is asked to pay.

So an invoice says: this is the plan you were on, this is what it costs, and
this is what you used. When overage pricing exists, the usage lines gain amounts
and nothing else about this file changes.

Three rules hold the whole thing together:

**Periods are billed in advance** (BILL-03). A renewal is the bill for the
period that is *starting*, at the terms of the plan version that governs it; the
usage lines report the period that just ended, for information. A purchase
covers the period it opens, so the first renewal is always for the period after
it and no period is billed twice.

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
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.db.models.billing import Plan, PlanVersion, Subscription
from app.db.models.invoice import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.integrations.billing.base import PaymentProvider
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.repositories.usage_repository import UsageEventRepository

if TYPE_CHECKING:
    from app.services.settlement_service import InvoiceSettlement

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
        plan: Plan | PlanVersion,
        period_start: datetime,
        period_end: datetime,
        now: datetime | None = None,
        plan_code: str | None = None,
        usage_since: datetime | None = None,
    ) -> tuple[Invoice, bool]:
        """Issue the renewal for a period. Returns the invoice and whether it is new.

        `plan` is the version whose terms the period is billed at (a bare
        `Plan` is accepted for callers that predate versioning). `usage_since`
        is the start of the period that just ended, whose consumption the
        usage lines report; without it they report `period_start` onwards.

        Idempotent by period: a sweep that runs twice finds the renewal it
        already issued and returns it unchanged.
        """
        moment = now if now is not None else datetime.now(UTC)
        existing = await self._invoices.get_for_period(period_start=period_start)
        if existing is not None:
            return existing, False

        version = plan if isinstance(plan, PlanVersion) else None
        lines = await self._lines(
            plan=plan,
            period_start=usage_since if usage_since is not None else period_start,
            period_end=period_start if usage_since is not None else period_end,
        )
        if version is not None:
            lines[0]["plan_version"] = version.version
            lines[0]["interval"] = version.interval.value
        code = plan_code if plan_code is not None else getattr(plan, "code", None)
        invoice = self._invoices.create(
            subscription_id=subscription.id,
            plan_code=str(code),
            amount_due=plan.price,
            currency=plan.currency,
            period_start=period_start,
            period_end=period_end,
            lines=lines,
            status=InvoiceStatus.OPEN if plan.price > 0 else InvoiceStatus.PAID,
            purpose=InvoicePurpose.RENEWAL,
            plan_version_id=version.id if version is not None else None,
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
                "plan": invoice.plan_code,
                "amount_due": str(invoice.amount_due),
            },
        )
        return invoice, True

    async def _lines(
        self,
        *,
        plan: Plan | PlanVersion,
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

        if period_end <= period_start:
            return lines
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
                "usage_from": period_start.isoformat(),
                "usage_until": period_end.isoformat(),
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

        await self._session.flush()
        if outcome.succeeded:
            await self._settlement().settle(invoice, payment=payment, now=moment)
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
        currency: str | None = None,
        expected_revision: int | None = None,
        recover_uncollectible: bool = False,
        actor: User | None = None,
    ) -> Payment:
        """Record money that arrived outside the system (BILL-10, BILL-20).

        A bank transfer, a card taken over the phone - asserted by platform
        staff who have *seen* the money. It is settled by exactly the engine a
        provider callback uses, so a full payment of a renewal restores a
        suspended workspace and a full payment of a purchase grants its plan.

        Refused, before anything is written:

        - an invoice that is not open - paying a paid invoice again, or a void
          one, is a 409; one written off as uncollectible needs the explicit
          `recover_uncollectible` flag;
        - a non-positive amount, a currency that is not the invoice's, or more
          than is outstanding - overpayment is not a feature of this system;
        - an `expected_revision` that is not the invoice's current one.
        """
        moment = now if now is not None else datetime.now(UTC)
        invoice = await self._invoices.require_by_id(invoice_id)
        if expected_revision is not None and invoice.revision != expected_revision:
            raise ConflictError(
                f"The invoice has changed (revision {invoice.revision}); reload it and retry."
            )
        if invoice.status is InvoiceStatus.VOID:
            raise ConflictError("A voided invoice cannot be paid.")
        if invoice.status is InvoiceStatus.PAID:
            raise ConflictError("This invoice has already been paid.")
        if invoice.status is InvoiceStatus.UNCOLLECTIBLE and not recover_uncollectible:
            raise ConflictError(
                "This invoice was written off as uncollectible. Recover it deliberately "
                "with recover_uncollectible to record a payment against it."
            )
        if invoice.status is InvoiceStatus.DRAFT:
            raise ConflictError("A draft invoice has not been issued and cannot be paid.")
        if amount <= 0:
            raise ValidationError("A payment must be for a positive amount.")
        if currency is not None and currency.upper() != invoice.currency.upper():
            raise ValidationError(f"This invoice is in {invoice.currency}.")
        if amount > invoice.outstanding:
            raise ValidationError(
                f"{amount} is more than the {invoice.outstanding} outstanding on this invoice."
            )

        payment = self._payments.record(
            invoice_id=invoice.id,
            status=PaymentStatus.SUCCEEDED,
            amount=amount,
            currency=invoice.currency,
            provider=provider,
            provider_reference=reference,
            processed_at=moment,
        )
        await self._session.flush()
        outcome, detail = await self._settlement().settle(
            invoice,
            payment=payment,
            now=moment,
            actor=actor,
            recover_uncollectible=recover_uncollectible,
        )
        if outcome != "applied":
            raise ConflictError(detail or "The payment could not be applied to this invoice.")
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

    def _settlement(self) -> InvoiceSettlement:
        # Imported here: the settlement engine imports the subscription service,
        # which is heavier than anything a read of invoices needs.
        from app.services.settlement_service import InvoiceSettlement

        return InvoiceSettlement(self._session, tenant_id=self._tenant_id)

    async def void(
        self,
        invoice_id: uuid.UUID,
        *,
        reason: str | None = None,
        subscription_policy: str | None = None,
        expected_revision: int | None = None,
        actor: User | None = None,
        now: datetime | None = None,
    ) -> Invoice:
        """Withdraw an invoice that should not have been issued (BILL-10, BILL-20).

        Voided rather than deleted or edited: the customer has seen it. Through
        the transition table and the settlement engine, which decides what the
        void means for the subscription - see `InvoiceSettlement.void`.
        """
        invoice = await self._invoices.require_by_id(invoice_id)
        if expected_revision is not None and invoice.revision != expected_revision:
            raise ConflictError(
                f"The invoice has changed (revision {invoice.revision}); reload it and retry."
            )
        return await self._settlement().void(
            invoice,
            reason=reason or "Voided.",
            subscription_policy=subscription_policy,
            now=now if now is not None else datetime.now(UTC),
            actor=actor,
        )

    async def list_invoices(self, *, limit: int = 50) -> list[Invoice]:
        return await self._invoices.list_invoices(limit=limit)

    async def get(self, invoice_id: uuid.UUID) -> Invoice:
        return await self._invoices.require_by_id(invoice_id)

    async def payments_for(self, invoice_id: uuid.UUID) -> list[Payment]:
        await self._invoices.require_by_id(invoice_id)
        return await self._payments.list_for_invoice(invoice_id)
