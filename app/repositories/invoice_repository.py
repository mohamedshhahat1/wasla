"""Data access for invoices and payments.

Scoped like every other workspace-owned table, with the usual platform-facing
exception kept in a separate class so it is visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select

from app.core.pagination import Cursor
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.repositories.base import BaseRepository, TenantScopedRepository


@dataclass(frozen=True, slots=True)
class RevenueTotal:
    """Money recognised in one currency, and how many invoices it came from."""

    currency: str
    amount: Decimal
    invoices: int


class InvoiceRepository(TenantScopedRepository[Invoice]):
    """One workspace's invoices."""

    model = Invoice

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Invoice.tenant_id == self.tenant_id

    async def get_by_id(self, invoice_id: uuid.UUID) -> Invoice | None:
        return await self._first(self._select().where(Invoice.id == invoice_id))

    async def require_by_id(self, invoice_id: uuid.UUID) -> Invoice:
        return await self._require(self._select().where(Invoice.id == invoice_id))

    async def get_for_period(self, *, period_start: datetime) -> Invoice | None:
        """The invoice already issued for this period, if there is one.

        The unique constraint is the real guard against billing a customer
        twice for March; this lookup exists so a second sweep is a no-op rather
        than an integrity error.
        """
        return await self._first(self._select().where(Invoice.period_start == period_start))

    async def list_invoices(
        self,
        *,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[Invoice]:
        """Newest first, which is the only order anybody reads invoices in."""
        statement = self._select().order_by(Invoice.period_start.desc(), Invoice.id.desc())
        if after is not None and after.sort_value is not None:
            statement = statement.where(
                (Invoice.period_start < after.sort_value)
                | ((Invoice.period_start == after.sort_value) & (Invoice.id < after.id))
            )
        return await self._all(statement.limit(limit))

    def create(
        self,
        *,
        subscription_id: uuid.UUID | None,
        plan_code: str,
        amount_due: Decimal,
        currency: str,
        period_start: datetime,
        period_end: datetime,
        lines: list[dict[str, object]],
        status: InvoiceStatus = InvoiceStatus.DRAFT,
    ) -> Invoice:
        return self.add(
            Invoice(
                tenant_id=self.tenant_id,
                subscription_id=subscription_id,
                status=status,
                plan_code=plan_code,
                amount_due=amount_due,
                currency=currency,
                period_start=period_start,
                period_end=period_end,
                lines=lines,
            )
        )


class PaymentRepository(TenantScopedRepository[Payment]):
    """One workspace's payment attempts."""

    model = Payment

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Payment.tenant_id == self.tenant_id

    async def get_by_id(self, payment_id: uuid.UUID) -> Payment | None:
        """One payment of this workspace's.

        Tenant-scoped like everything on this repository, which is what makes a
        callback naming another workspace's payment resolve to nothing rather
        than to that payment. The scoping is the isolation boundary here, not a
        convenience.
        """
        return await self._first(self._select().where(Payment.id == payment_id))

    async def list_for_invoice(self, invoice_id: uuid.UUID) -> list[Payment]:
        """Every attempt, oldest first: the history is the point.

        Two attempts written in the same transaction share a `created_at` -
        PostgreSQL's `now()` is fixed for the transaction - so their order
        between themselves falls to a random primary key. That is a limitation
        of the ordering rather than of the record: real attempts are separated
        by a customer updating a card, and no attempt is ever lost.
        """
        return await self._all(
            self._select()
            .where(Payment.invoice_id == invoice_id)
            .order_by(Payment.created_at, Payment.id)
        )

    async def get_by_idempotency_key(self, key: str) -> Payment | None:
        """A payment already created for this caller's request key.

        Tenant-scoped, matching the unique constraint: one workspace's key
        never finds another's attempt.
        """
        return await self._first(self._select().where(Payment.idempotency_key == key))

    async def get_by_transaction(self, *, provider: str, transaction_id: str) -> Payment | None:
        """The attempt a provider transaction settled, if we have it.

        The fallback path for a reversal callback. A refund names the
        transaction it reverses rather than carrying our own reference home, so
        this is how such an event is tied back to a payment - still by an
        identifier we recorded ourselves when the money arrived, and still
        tenant-scoped.
        """
        return await self._first(
            self._select()
            .where(Payment.provider == provider)
            .where(Payment.provider_reference == transaction_id)
        )

    async def get_by_reference(self, *, provider: str, reference: str) -> Payment | None:
        """Find an attempt by the provider's own identifier.

        What makes a repeated webhook a no-op instead of a second payment.
        """
        return await self._first(
            self._select()
            .where(Payment.provider == provider)
            .where(Payment.provider_reference == reference)
        )

    def record(
        self,
        *,
        invoice_id: uuid.UUID,
        status: PaymentStatus,
        amount: Decimal,
        currency: str,
        provider: str,
        provider_reference: str | None = None,
        failure_reason: str | None = None,
        processed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Payment:
        return self.add(
            Payment(
                tenant_id=self.tenant_id,
                invoice_id=invoice_id,
                status=status,
                amount=amount,
                currency=currency,
                provider=provider,
                provider_reference=provider_reference,
                failure_reason=failure_reason,
                processed_at=processed_at,
                idempotency_key=idempotency_key,
                refunded_amount=Decimal("0.00"),
            )
        )


class PlatformInvoiceRepository(BaseRepository[Invoice]):
    """Invoices across every workspace, for platform revenue reporting.

    Deliberately unscoped and deliberately its own class, like the platform
    usage and subscription readers.
    """

    model = Invoice

    async def revenue(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[RevenueTotal]:
        """What was actually collected in a window, by currency.

        Grouped by currency rather than summed into one figure: adding dollars
        to euros produces a number that is wrong in a way nobody can see. Only
        `paid` invoices count - an issued invoice is a hope, not revenue.
        """
        statement = (
            select(
                Invoice.currency,
                func.coalesce(func.sum(Invoice.amount_paid), 0),
                func.count(),
            )
            .where(Invoice.status == InvoiceStatus.PAID)
            .group_by(Invoice.currency)
            .order_by(Invoice.currency)
        )
        if since is not None:
            statement = statement.where(Invoice.paid_at >= since)
        if until is not None:
            statement = statement.where(Invoice.paid_at < until)

        result = await self.session.execute(statement)
        return [
            RevenueTotal(currency=row[0], amount=Decimal(row[1]), invoices=int(row[2]))
            for row in result.all()
        ]

    async def outstanding(self) -> list[RevenueTotal]:
        """What has been billed and not paid, by currency."""
        statement = (
            select(
                Invoice.currency,
                func.coalesce(func.sum(Invoice.amount_due - Invoice.amount_paid), 0),
                func.count(),
            )
            .where(Invoice.status == InvoiceStatus.OPEN)
            .group_by(Invoice.currency)
            .order_by(Invoice.currency)
        )
        result = await self.session.execute(statement)
        return [
            RevenueTotal(currency=row[0], amount=Decimal(row[1]), invoices=int(row[2]))
            for row in result.all()
        ]

    async def collectible(self, *, before: datetime, limit: int = 200) -> Sequence[Invoice]:
        """Open invoices an automatic charge may be attempted against.

        Deliberately a *wide* net rather than a precise one. Everything about
        whether a particular invoice should be charged - a live subscription, a
        usable card, an attempt budget that is not spent - is decided by
        `RecurringService`, because those are billing rules and belong with the
        rules. This query's only job is to avoid loading the whole table.

        `next_collection_at IS NULL` is included on purpose: that is the state
        of an invoice nobody has tried yet, which is exactly the one a first
        attempt is for. An invoice whose attempts have run out also has NULL
        there, and is filtered by the attempt count in the service.

        Ordered oldest-first so the longest-outstanding money is chased first,
        and so a backlog drains in a predictable order rather than by whichever
        rows the planner happens to return.
        """
        return await self._all(
            self._select()
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.subscription_id.is_not(None))
            .where((Invoice.next_collection_at.is_(None)) | (Invoice.next_collection_at <= before))
            .order_by(Invoice.period_start)
            .limit(limit)
        )

    async def overdue(self, *, before: datetime, limit: int = 200) -> Sequence[Invoice]:
        """Open invoices issued before a moment, oldest first.

        What the dunning sweep reads. Keyed on `issued_at` rather than
        `period_start`, because the grace a customer gets should run from the
        day they were asked for money - an invoice for a period that ended
        weeks ago is not weeks overdue if it was only issued yesterday.

        Invoices with no `issued_at` are excluded. Those are drafts and
        checkout-created rows that nobody has been billed for, and chasing
        somebody for a bill they were never sent is worse than not chasing.
        """
        return await self._all(
            self._select()
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.issued_at.is_not(None))
            .where(Invoice.issued_at < before)
            .where(Invoice.subscription_id.is_not(None))
            .order_by(Invoice.issued_at)
            .limit(limit)
        )

    async def due_for_period(
        self,
        *,
        period_start: datetime,
        limit: int = 200,
    ) -> Sequence[Invoice]:
        """Open invoices for a period, for a collection sweep."""
        return await self._all(
            self._select()
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.period_start == period_start)
            .order_by(Invoice.period_start)
            .limit(limit)
        )
