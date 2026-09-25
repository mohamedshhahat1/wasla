"""Billing incidents: money-shaped problems a person has to look at.

Before this table, every one of these was a log line (BILL-15). A customer who
paid twice produced `billing.settlement_refused` at warning and nothing else: no
row an operator could list, no state that said whether anybody had dealt with
it, no alert that could fire on it. A log is where evidence goes to be rotated
away, and "we took this customer's money twice" is not a fact a billing system
may only remember for fourteen days.

An incident is written in the same transaction as the decision that raised it,
so it cannot be lost to a crash between the two, and it is never deleted: it is
*resolved*, by a named person, with a note. Nothing here moves money. A duplicate
payment is flagged rather than refunded automatically - whether to refund, and
how much, is a decision about a customer, and the provider behaviour around
automatic reversal is not unambiguous enough to make it silently.

What is deliberately never stored: any part of a provider payload, a card
number, a token or a billing address. `detail` is written by this application,
bounded, and names figures and rules - never data a customer or a provider sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import CURRENCY_LENGTH, MAX_BILLING_REASON_LENGTH
from app.db.models.enums import _enum_type

MAX_INCIDENT_DETAIL_LENGTH: Final = 300
MAX_INCIDENT_KEY_LENGTH: Final = 250


class BillingIncidentKind(StrEnum):
    """What went wrong, from a closed vocabulary an alert can filter on.

    ``DUPLICATE_PAYMENT``
        A provider collected money for an invoice that was already settled, or
        for a plan the workspace had already bought for the period. The money
        is real; the invoice cannot take it (BILL-15).
    ``REFUSED_SETTLEMENT``
        A success arrived that the rules would not apply - a payment page paid
        after the subscription was cancelled, for example. Money held, nothing
        granted, somebody must decide.
    ``MISMATCHED_CALLBACK``
        An authenticated callback that disagreed with what we asked for: the
        wrong amount, currency, Paymob order, integration or mode (BILL-11).
    ``UNKNOWN_CALLBACK``
        An authenticated callback naming nothing this system issued.
    ``PERMANENT_PROVIDER_ERROR``
        The provider refused an automatic charge request as malformed - a
        configuration problem that retrying cannot fix (BILL-05).
    ``RECOVERED_BY_RECONCILIATION``
        A payment whose callback never arrived, settled from a provider
        inquiry instead (BILL-09). Recorded so the recovery is visible, and
        resolved on creation: nothing is owed.
    ``REFUND_REQUESTED``
        A workspace owner asked for money back. The request moves nothing; a
        platform operator decides (BILL-13).

    The top-up and custom-plan kinds (ADR-113):

    ``TOPUP_PAID_BUT_NOT_GRANTED``
        A top-up was paid and its allowance could not be granted - the period
        it was bought for had already ended, or the subscription is no longer
        serving. Money held, nothing granted; an operator refunds or grants.
    ``TOPUP_DUPLICATE_PAYMENT``
        Money arrived for a top-up invoice that was already settled.
    ``TOPUP_REFUND_AFTER_CONSUMPTION``
        A granted top-up was refunded after part of the allowance it added was
        already used. Nothing is withdrawn automatically; an operator decides.
    ``TOPUP_ENTITLEMENT_REVERSAL_BLOCKED``
        A granted top-up was refunded and its allowance was *not* withdrawn,
        because withdrawal is never automatic. An operator decides.
    ``TOPUP_UNKNOWN_CALLBACK``
        A top-up invoice was settled with no purchase behind it - a state the
        checkout path cannot produce, raised rather than ignored.
    ``CUSTOM_PLAN_SCOPE_MISMATCH``
        Something tried to put a workspace on another workspace's custom plan
        and was refused.
    """

    DUPLICATE_PAYMENT = "duplicate_payment"
    REFUSED_SETTLEMENT = "refused_settlement"
    MISMATCHED_CALLBACK = "mismatched_callback"
    UNKNOWN_CALLBACK = "unknown_callback"
    PERMANENT_PROVIDER_ERROR = "permanent_provider_error"
    RECOVERED_BY_RECONCILIATION = "recovered_by_reconciliation"
    REFUND_REQUESTED = "refund_requested"
    TOPUP_PAID_BUT_NOT_GRANTED = "topup_paid_but_not_granted"
    TOPUP_DUPLICATE_PAYMENT = "topup_duplicate_payment"
    TOPUP_REFUND_AFTER_CONSUMPTION = "topup_refund_after_consumption"
    TOPUP_ENTITLEMENT_REVERSAL_BLOCKED = "topup_entitlement_reversal_blocked"
    TOPUP_UNKNOWN_CALLBACK = "topup_unknown_callback"
    CUSTOM_PLAN_SCOPE_MISMATCH = "custom_plan_scope_mismatch"


class BillingIncidentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


BILLING_INCIDENT_KIND_TYPE = _enum_type(BillingIncidentKind, name="billing_incident_kind")
BILLING_INCIDENT_STATUS_TYPE = _enum_type(BillingIncidentStatus, name="billing_incident_status")


class BillingIncident(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One money-shaped problem, from being noticed to being resolved."""

    __tablename__ = "billing_incidents"
    __table_args__ = (
        Index("ix_billing_incidents_tenant_id", "tenant_id"),
        Index("ix_billing_incidents_status_kind", "status", "kind"),
        Index("ix_billing_incidents_payment_id", "payment_id"),
        # One incident per fact. The key is built by the raiser from the
        # identifiers of the thing that happened - a provider transaction, a
        # payment - so a callback delivered three times raises one incident.
        UniqueConstraint("dedupe_key", name="uq_billing_incidents_dedupe_key"),
    )

    # Nullable: an unknown callback has, by definition, no workspace. RESTRICT
    # otherwise, for the reason every other ledger table has it (BILL-19).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kind: Mapped[BillingIncidentKind] = mapped_column(BILLING_INCIDENT_KIND_TYPE, nullable=False)
    status: Mapped[BillingIncidentStatus] = mapped_column(
        BILLING_INCIDENT_STATUS_TYPE,
        nullable=False,
    )
    dedupe_key: Mapped[str] = mapped_column(String(MAX_INCIDENT_KEY_LENGTH), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=True,
    )
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(CURRENCY_LENGTH), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(MAX_INCIDENT_DETAIL_LENGTH), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolution_note: Mapped[str | None] = mapped_column(
        String(MAX_BILLING_REASON_LENGTH),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"BillingIncident(kind={self.kind!r}, status={self.status!r})"
