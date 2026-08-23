"""Invoices and payments: what was owed for a period, and what was paid.

An invoice is a **record of a past period**, not a live calculation. Once issued
it stops moving: the plan can change, a price can be edited, usage can keep
accruing, and last month's invoice still says what last month said. That is the
entire reason this table exists rather than a function that adds things up on
demand — a figure recomputed from today's configuration cannot answer "why was I
charged this in March", which is the only question anybody ever asks about an
invoice.

So the amounts are copied, not referenced. `plan_code` and the line amounts are
written onto the row at issue time; nothing here joins back to `plans` to render
a total.

A payment is an attempt, not a state. Attempts fail and are retried, and each
one is a row: collapsing them into a single status on the invoice would lose the
history a dispute turns on, which is exactly the history a chargeback needs.

Nothing here moves money. `provider` and `provider_reference` are where a real
payment processor will identify its own objects; until then invoices are issued
and marked paid by the platform, which is what makes local development and every
test in this suite possible without credentials (ADR-031).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import CURRENCY_LENGTH, DEFAULT_CURRENCY
from app.db.models.enums import _enum_type

MAX_REFERENCE_LENGTH: Final = 200
MAX_DESCRIPTION_LENGTH: Final = 300
# Long enough for a provider's decline text without becoming a place somebody
# stores a stack trace.
MAX_FAILURE_LENGTH: Final = 500


class InvoiceStatus(StrEnum):
    """Where an invoice stands.

    `DRAFT` exists so an invoice can be assembled and checked before anybody is
    asked for money. `VOID` is how a mistake is undone: an issued invoice is
    never deleted and never edited, because the customer has seen it.
    """

    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


class PaymentStatus(StrEnum):
    """What happened to one attempt at collecting."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


# Statuses in which an invoice is finished and will not change again.
TERMINAL_INVOICE_STATUSES: Final[frozenset[InvoiceStatus]] = frozenset(
    {
        InvoiceStatus.PAID,
        InvoiceStatus.UNCOLLECTIBLE,
        InvoiceStatus.VOID,
    }
)

INVOICE_STATUS_TYPE = _enum_type(InvoiceStatus, name="invoice_status")
PAYMENT_STATUS_TYPE = _enum_type(PaymentStatus, name="payment_status")


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What one workspace owed for one period."""

    __tablename__ = "invoices"
    __table_args__ = (
        # One invoice per workspace per period. A sweep that runs twice, or two
        # replicas sweeping at once, must not bill a customer twice for March -
        # and that is a constraint's job rather than a check in a service.
        UniqueConstraint(
            "tenant_id",
            "period_start",
            name="uq_invoices_tenant_id_period_start",
        ),
        Index("ix_invoices_tenant_id", "tenant_id"),
        Index("ix_invoices_tenant_id_status", "tenant_id", "status"),
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_subscription_id", "subscription_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL: an invoice outlives the subscription it came from. A
        # customer who left last year can still be shown what they paid.
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[InvoiceStatus] = mapped_column(INVOICE_STATUS_TYPE, nullable=False)
    # Copied from the plan at issue time, never joined for. A plan renamed or
    # repriced afterwards must not change what March says.
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The lines as they were, including the usage figures behind them. JSONB
    # rather than a child table because nothing queries inside a line: an
    # invoice is read whole, by one customer, to answer one question.
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_INVOICE_STATUSES

    @property
    def outstanding(self) -> Decimal:
        """What is still owed. Never negative: an overpayment is a credit, and
        a credit is a decision this system does not make yet."""
        remaining = self.amount_due - self.amount_paid
        return remaining if remaining > 0 else Decimal("0.00")

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Invoice(tenant_id={self.tenant_id!r}, status={self.status!r})"


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One attempt at collecting an invoice.

    Attempts are rows rather than a status, because a failed one is not
    forgotten when a later one succeeds: the history is what a dispute, a
    chargeback and an angry email all turn on.
    """

    __tablename__ = "payments"
    __table_args__ = (
        # A provider's own idempotency key. Two webhooks describing the same
        # charge must not become two payments, and a retried request must not
        # collect twice.
        UniqueConstraint(
            "provider",
            "provider_reference",
            name="uq_payments_provider_provider_reference",
        ),
        Index("ix_payments_tenant_id", "tenant_id"),
        Index("ix_payments_invoice_id", "invoice_id"),
        Index("ix_payments_status", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[PaymentStatus] = mapped_column(PAYMENT_STATUS_TYPE, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # What the provider said when it refused. Kept because "declined" alone
    # tells a customer nothing they can act on.
    failure_reason: Mapped[str | None] = mapped_column(String(MAX_FAILURE_LENGTH), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Payment(invoice_id={self.invoice_id!r}, status={self.status!r})"
