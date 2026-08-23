"""Billing actions and figures that belong to the platform, not to a workspace.

Two kinds of thing live here, and both are here for the same reason: a workspace
must not be able to do them.

**Acting on an invoice.** Recording a payment is an assertion that somebody has
seen the money; voiding one is a decision to withdraw a bill. A customer able to
do either is a customer who pays nothing. The invoice is resolved across
workspaces first, and then acted on through the *tenant-scoped* service, so
everything after the lookup is confined to the workspace that owns it.

**Reading revenue.** What was actually collected, grouped by currency, because
adding dollars to euros produces a number that is wrong in a way nobody can see.

What is still absent is MRR and ARR. Both are projections of what subscriptions
*will* produce, and the honest version needs decisions nobody has made — whether
a trial counts, what a past-due subscription is worth, how an annual plan is
spread. Collected revenue is a fact; those are estimates, and an estimate on a
dashboard becomes a number somebody quotes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.invoice import Invoice, Payment
from app.db.models.user import User
from app.repositories.invoice_repository import PlatformInvoiceRepository, RevenueTotal
from app.services.audit_service import AuditTrail
from app.services.invoice_service import InvoiceService


class PlatformBillingService:
    """Invoice administration and revenue reporting across every workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._invoices = PlatformInvoiceRepository(session)

    async def _locate(self, invoice_id: uuid.UUID) -> Invoice:
        """Find an invoice in whichever workspace owns it.

        The one unscoped lookup in this file. Everything afterwards goes through
        the tenant-scoped service built from what it found.
        """
        invoice = await self._session.scalar(select(Invoice).where(Invoice.id == invoice_id))
        if invoice is None:
            raise NotFoundError("No such invoice.")
        return invoice

    def _service(self, invoice: Invoice) -> InvoiceService:
        return InvoiceService(self._session, tenant_id=invoice.tenant_id)

    async def record_payment(
        self,
        *,
        invoice_id: uuid.UUID,
        amount: Decimal,
        provider: str,
        reference: str | None = None,
        actor: User | None = None,
    ) -> Payment:
        invoice = await self._locate(invoice_id)
        payment = await self._service(invoice).record_payment(
            invoice_id=invoice.id,
            amount=amount,
            provider=provider,
            reference=reference,
        )
        # Recorded against the *workspace's* trail, not the platform's, and
        # attributed to the staff member. The customer is entitled to see who
        # marked their invoice paid, which is the whole reason this is logged.
        self._audit(invoice).record(
            AuditAction.PAYMENT_RECORDED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="invoice",
            target_id=invoice.id,
            meta={"amount": str(amount), "provider": provider},
        )
        return payment

    async def void(
        self,
        invoice_id: uuid.UUID,
        *,
        reason: str | None = None,
        actor: User | None = None,
    ) -> Invoice:
        invoice = await self._locate(invoice_id)
        voided = await self._service(invoice).void(invoice.id, reason=reason)
        self._audit(invoice).record(
            AuditAction.INVOICE_VOIDED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="invoice",
            target_id=invoice.id,
            meta={"reason": reason} if reason else None,
        )
        return voided

    def _audit(self, invoice: Invoice) -> AuditTrail:
        return AuditTrail(self._session, tenant_id=invoice.tenant_id)

    async def revenue(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[RevenueTotal]:
        """What was collected in a window. Paid invoices only: an issued
        invoice is a hope, not revenue."""
        return await self._invoices.revenue(since=since, until=until)

    async def outstanding(self) -> list[RevenueTotal]:
        """What has been billed and not yet paid."""
        return await self._invoices.outstanding()
