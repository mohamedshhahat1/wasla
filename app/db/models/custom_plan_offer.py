"""A custom plan offered to its workspace, and what the customer did with it.

A custom plan (ADR-113) is an ordinary tenant-scoped `Plan` with immutable
`PlanVersion`s. Creating one grants nothing. A *priced* custom plan reaches its
workspace through an **offer** the workspace's owner accepts and pays for, a
manual payment an operator has seen, or a complimentary grant said out loud -
never merely because somebody on the platform assigned it (ADR-114).

An offer names one immutable version, so what the customer is shown - price,
currency, interval and the seven limits - is exactly what they pay for and
exactly what they get. It moves through a small state machine:

    offered --accept--> pending_payment --authenticated payment--> active
       |                      |
       +--decline/expire/cancel--+--> declined | expired | cancelled

``OFFERED``
    Visible to the workspace's owners with its full terms. Nothing is owed.
``PENDING_PAYMENT``
    An owner accepted and a hosted checkout was opened: a `CHECKOUT` invoice
    pinned to the offer's version and naming this offer. Accepting again opens
    another page for the same terms; whichever is paid first activates it.
``ACTIVE``
    A signed provider callback (or a provider inquiry recovering a lost one)
    settled one of its invoices, and settlement put the subscription on the
    offered version. The only way in.
``DECLINED``
    An owner said no. A page opened earlier and paid anyway is refused at
    settlement: the money is held and an incident raised.
``EXPIRED``
    Its `expires_at` passed before it was accepted - or after it was accepted
    but before payment. A page *opened in time* may still be paid: the checkout
    snapshot is what the customer agreed to.
``CANCELLED``
    Withdrawn by platform staff. Money for it afterwards is held, like a decline.

There is no ``DRAFT`` state: a custom plan that nobody has offered yet is
simply a plan with no offer.

At most one offer per workspace is open (offered or pending payment) at a
time, which a partial unique index enforces. Its tenant, plan and version are
fixed for ever by a trigger; only its state moves.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, RevisionedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import MAX_BILLING_REASON_LENGTH
from app.db.models.enums import _enum_type


class CustomPlanOfferStatus(StrEnum):
    """Where one offer stands. See the module docstring."""

    OFFERED = "offered"
    PENDING_PAYMENT = "pending_payment"
    ACTIVE = "active"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# The offers a customer can still act on, and the ones the partial unique
# index keeps to one per workspace. Named once: the index, the sweep and the
# service must mean the same set.
OPEN_OFFER_STATUSES: Final[frozenset[CustomPlanOfferStatus]] = frozenset(
    {CustomPlanOfferStatus.OFFERED, CustomPlanOfferStatus.PENDING_PAYMENT}
)
_OPEN_SQL: Final = "status IN ('offered', 'pending_payment')"

OFFER_TRANSITIONS: Final[dict[CustomPlanOfferStatus, frozenset[CustomPlanOfferStatus]]] = {
    CustomPlanOfferStatus.OFFERED: frozenset(
        {
            CustomPlanOfferStatus.PENDING_PAYMENT,
            CustomPlanOfferStatus.DECLINED,
            CustomPlanOfferStatus.EXPIRED,
            CustomPlanOfferStatus.CANCELLED,
        }
    ),
    CustomPlanOfferStatus.PENDING_PAYMENT: frozenset(
        {
            CustomPlanOfferStatus.ACTIVE,
            CustomPlanOfferStatus.DECLINED,
            CustomPlanOfferStatus.EXPIRED,
            CustomPlanOfferStatus.CANCELLED,
        }
    ),
    # A page opened before the offer expired may still be paid (the snapshot
    # is authoritative), and settling it is how an expired offer activates.
    CustomPlanOfferStatus.EXPIRED: frozenset({CustomPlanOfferStatus.ACTIVE}),
    CustomPlanOfferStatus.ACTIVE: frozenset(),
    CustomPlanOfferStatus.DECLINED: frozenset(),
    CustomPlanOfferStatus.CANCELLED: frozenset(),
}


def offer_may_move(current: CustomPlanOfferStatus, target: CustomPlanOfferStatus) -> bool:
    """Whether an offer may go from `current` to `target`."""
    return target in OFFER_TRANSITIONS[current]


CUSTOM_PLAN_OFFER_STATUS_TYPE = _enum_type(CustomPlanOfferStatus, name="custom_plan_offer_status")


class CustomPlanOffer(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """One custom plan version offered to the workspace that owns the plan."""

    __tablename__ = "custom_plan_offers"
    __table_args__ = (
        # The target of `invoices (custom_plan_offer_id, tenant_id)`: an
        # invoice can only ever name an offer of its own workspace.
        UniqueConstraint("id", "tenant_id", name="uq_custom_plan_offers_id_tenant_id"),
        Index(
            "uq_custom_plan_offers_one_open_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text(_OPEN_SQL),
        ),
        Index("ix_custom_plan_offers_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_custom_plan_offers_plan_version_id", "plan_version_id"),
        # The expiry sweep.
        Index("ix_custom_plan_offers_status_expires_at", "status", "expires_at"),
        CheckConstraint(
            "status NOT IN ('pending_payment', 'active') OR accepted_at IS NOT NULL",
            name="accepted_has_moment",
        ),
        CheckConstraint("status <> 'active' OR activated_at IS NOT NULL", name="active_has_moment"),
        CheckConstraint(
            "status <> 'declined' OR declined_at IS NOT NULL", name="declined_has_moment"
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL", name="cancelled_has_moment"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT (BILL-19): an offer is part of the commercial record.
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[CustomPlanOfferStatus] = mapped_column(
        CUSTOM_PLAN_OFFER_STATUS_TYPE, nullable=False, default=CustomPlanOfferStatus.OFFERED
    )
    # After this, the offer can no longer be accepted. Null: open until acted on.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why the operator made it - required, like every platform billing change.
    reason: Mapped[str] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decline_reason: Mapped[str | None] = mapped_column(
        String(MAX_BILLING_REASON_LENGTH), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_OFFER_STATUSES

    def accepting_is_open(self, moment: datetime) -> bool:
        """Whether an owner may accept (open a page for) this offer at `moment`."""
        return self.is_open and (self.expires_at is None or moment < self.expires_at)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"CustomPlanOffer(tenant_id={self.tenant_id!r}, status={self.status!r})"


# What was offered never changes: only the state moves. And an offer is always
# of the offering workspace's own custom plan, at a version of that plan.
# Restated verbatim by migration 0073.
OFFER_INTEGRITY_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION custom_plan_offers_refuse_foreign_or_changed() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND (
               NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
            OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
        ) THEN
            RAISE EXCEPTION 'a custom plan offer keeps the terms it was made with'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM plans p JOIN plan_versions v ON v.plan_id = p.id
             WHERE p.id = NEW.plan_id
               AND v.id = NEW.plan_version_id
               AND p.scope = 'tenant'
               AND p.tenant_id = NEW.tenant_id
        ) THEN
            RAISE EXCEPTION 'custom_plan_not_available_for_workspace'
                USING ERRCODE = 'integrity_constraint_violation',
                      DETAIL = 'An offer names its own workspace''s custom plan and version.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
OFFER_INTEGRITY_TRIGGER_SQL: Final = (
    "CREATE TRIGGER custom_plan_offers_integrity BEFORE INSERT OR UPDATE ON custom_plan_offers "
    "FOR EACH ROW EXECUTE FUNCTION custom_plan_offers_refuse_foreign_or_changed()"
)
# An invoice that names an offer sells exactly the offered version. The
# composite foreign key already keeps it inside its workspace.
INVOICE_OFFER_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION invoices_refuse_offer_mismatch() RETURNS trigger AS $$
    BEGIN
        IF NEW.custom_plan_offer_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM custom_plan_offers o
             WHERE o.id = NEW.custom_plan_offer_id
               AND o.plan_version_id = NEW.plan_version_id
        ) THEN
            RAISE EXCEPTION 'an invoice for a custom plan offer sells the offered version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.custom_plan_offer_id IS DISTINCT FROM OLD.custom_plan_offer_id THEN
            RAISE EXCEPTION 'an invoice keeps the offer it was opened for'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
INVOICE_OFFER_TRIGGER_SQL: Final = (
    "CREATE TRIGGER invoices_custom_plan_offer BEFORE INSERT OR UPDATE OF "
    "custom_plan_offer_id, plan_version_id ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_offer_mismatch()"
)

for _statement in (OFFER_INTEGRITY_FUNCTION_SQL, OFFER_INTEGRITY_TRIGGER_SQL):
    event.listen(CustomPlanOffer.__table__, "after_create", DDL(_statement))  # type: ignore[no-untyped-call]


__all__ = [
    "CUSTOM_PLAN_OFFER_STATUS_TYPE",
    "INVOICE_OFFER_FUNCTION_SQL",
    "INVOICE_OFFER_TRIGGER_SQL",
    "OFFER_TRANSITIONS",
    "OPEN_OFFER_STATUSES",
    "CustomPlanOffer",
    "CustomPlanOfferStatus",
    "offer_may_move",
]
