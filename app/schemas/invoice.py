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
from app.db.models.payment_method import PaymentMethod


def _money(amount: Decimal) -> str:
    """Two decimal places, always, so a total never renders as "99.0"."""
    return f"{amount:.2f}"


def _money_or_zero(amount: Decimal | None) -> str:
    """The same, for a column whose default has not been applied yet.

    `refunded_amount` is `NOT NULL` with a default, and SQLAlchemy applies
    defaults at INSERT - so a `Payment` that has been constructed but not
    flushed reads as None. Zero is the truthful rendering of that state rather
    than a papering-over: nothing has been given back on a row that does not
    exist yet.
    """
    return _money(amount if amount is not None else Decimal("0.00"))


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
    """One attempt at collecting, and where it has got to.

    This is what a client polls after sending somebody to a payment page. The
    provider redirects the customer back with the result in the query string,
    and **that is not evidence** - anybody can visit a URL with `success=true`
    on it. A client showing a customer whether they paid must ask for this,
    because this is derived from a signed callback the provider sent us
    directly (ADR-044).

    `refund_pending` is the honest middle state: a reversal the provider has
    accepted but not yet confirmed. A customer in that state has been refunded
    from our side and has not seen the money, and a page that showed either
    "refunded" or "paid" would be wrong.
    """

    id: str
    status: PaymentStatus
    amount: str
    currency: str
    provider: str
    invoice_id: str
    refunded_amount: str
    refund_pending: bool
    refunded_at: datetime | None
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
            invoice_id=str(payment.invoice_id),
            refunded_amount=_money_or_zero(payment.refunded_amount),
            # Asked for, not yet confirmed by a callback. Reported as a
            # boolean rather than by exposing the provider's reference: which
            # transaction id a reversal has is nobody's business outside
            # support, and it is in the audit log for them.
            refund_pending=bool(payment.refund_requested_at) and not payment.refunded_amount,
            refunded_at=payment.refunded_at,
            failure_reason=payment.failure_reason,
            processed_at=payment.processed_at,
            created_at=payment.created_at,
        )


class PaymentMethodRead(BaseModel):
    """A saved card, as a customer needs to recognise it.

    Deliberately not the token. That is what charges the card, it is useless to
    a client, and a response carrying it would be one more place it could be
    logged or cached. What a person needs is which of their cards this is.
    """

    id: str
    brand: str | None
    masked_pan: str | None
    is_default: bool
    created_at: datetime

    @classmethod
    def from_model(cls, method: PaymentMethod) -> Self:
        return cls(
            id=str(method.id),
            brand=method.brand,
            masked_pan=method.masked_pan,
            is_default=method.is_default,
            created_at=method.created_at,
        )


class RefundRequestPayload(BaseModel):
    """Asking for a payment to be given back.

    A reason and nothing else. There is deliberately no `amount`: it is the
    payment's own unreturned balance, computed on the server, so there is no
    field anybody can send to be refunded more than they paid.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=300)


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
    "PaymentMethodRead",
    "PaymentRead",
    "PaymentRecordRequest",
    "RefundRequestPayload",
]
