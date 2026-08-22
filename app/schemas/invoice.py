"""Invoice API contracts.

Amounts cross the wire as strings, not floats. A JSON number is a double in most
clients, and 19.99 does not survive that trip intact — which is a rounding error
in something a customer is being asked to pay. A string is exact and every
client can parse it into whatever decimal type it has.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus


def _money(amount: Decimal) -> str:
    """Two decimal places, always, so a total never renders as "99.0"."""
    return f"{amount:.2f}"


class InvoiceLineRead(BaseModel):
    """One line of an invoice, as it was written at issue time.

    `amount` is present on every line including usage lines, where it is zero:
    no per-unit overage price is stored anywhere, and the shape of a line should
    not change on the day one is.
    """

    kind: str
    description: str
    quantity: int
    amount: str
    unit: str | None = None


class PaymentRead(BaseModel):
    """One attempt at collecting."""

    id: str
    status: PaymentStatus
    amount: str
    currency: str
    provider: str
    failure_reason: str | None
    processed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, payment: Payment) -> Self:
        return cls(
            id=str(payment.id),
            status=payment.status,
            amount=_money(payment.amount),
            currency=payment.currency,
            provider=payment.provider,
            failure_reason=payment.failure_reason,
            processed_at=payment.processed_at,
            created_at=payment.created_at,
        )


class InvoiceRead(BaseModel):
    """What a workspace owed for one period, and what is still outstanding."""

    id: str
    status: InvoiceStatus
    plan_code: str
    amount_due: str
    amount_paid: str
    outstanding: str
    currency: str
    period_start: datetime
    period_end: datetime
    issued_at: datetime | None
    paid_at: datetime | None
    lines: list[InvoiceLineRead]

    @classmethod
    def from_model(cls, invoice: Invoice) -> Self:
        return cls(
            id=str(invoice.id),
            status=invoice.status,
            plan_code=invoice.plan_code,
            amount_due=_money(invoice.amount_due),
            amount_paid=_money(invoice.amount_paid),
            outstanding=_money(invoice.outstanding),
            currency=invoice.currency,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            issued_at=invoice.issued_at,
            paid_at=invoice.paid_at,
            lines=[_line(line) for line in invoice.lines],
        )


def _line(raw: dict[str, Any]) -> InvoiceLineRead:
    """Read one stored line defensively.

    The lines were written by an earlier version of this code and are never
    migrated, which is the point of storing them — so a field added later must
    not make an old invoice unreadable.
    """
    return InvoiceLineRead(
        kind=str(raw.get("kind", "usage")),
        description=str(raw.get("description", "")),
        quantity=int(raw.get("quantity", 0)),
        amount=str(raw.get("amount", "0.00")),
        unit=str(raw["unit"]) if raw.get("unit") else None,
    )


class PaymentRecordRequest(BaseModel):
    """Recording money that arrived outside the system.

    A bank transfer, a card taken over the phone. Platform staff only: it is an
    assertion that somebody has seen the money.
    """

    model_config = ConfigDict(extra="forbid")

    # A string, for the same reason amounts leave as strings: a float here would
    # be a rounding error somebody has to reconcile.
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    provider: str = Field(min_length=1, max_length=50)
    reference: str | None = Field(default=None, max_length=200)


class InvoiceVoidRequest(BaseModel):
    """Withdrawing an invoice that should not have been issued."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=300)


__all__ = [
    "InvoiceLineRead",
    "InvoiceRead",
    "InvoiceVoidRequest",
    "PaymentRead",
    "PaymentRecordRequest",
]
