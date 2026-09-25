"""Top-ups: one-time extra allowance on top of a workspace's plan (ADR-113).

Two tables, and the line between them is the line between a price list and a
ledger.

`topup_products` is the catalogue - "AI Turns +10K for 200 EGP" - edited by
platform staff. A product may be offered to every workspace (`GLOBAL`) or
written for one (`TENANT`), and a tenant product is invisible, unpurchasable and
ungrantable anywhere else.

`topup_purchases` is what a workspace actually holds. A purchase **freezes** what
the customer was shown when the checkout opened - the product's name and code,
the entitlement, the quantity, the price, the period and the expiry - and a
trigger refuses any later change to those columns, so editing or retiring a
product can never change what somebody already paid for. A platform operator's
complimentary grant is a row here too, with `source = platform_grant`, and the
database refuses to let one carry an invoice, a payment or a price: free
allowance is said out loud, never disguised as a sale.

**A top-up never changes the plan.** It is not a plan version, a wallet balance
or a credit: it adds `quantity` to one entitlement from the moment it is granted
until `expires_at`, which is the end of the billing period it was bought in.
There is no carry-over. Capacity top-ups (numbers, seats, documents, storage)
expire the same way, and nothing is deleted when they do - a workspace left
above its limit keeps what it has and cannot add more.

The effective limit a workspace is held to is therefore

    pinned plan version  +  active purchased top-ups  +  active platform grants

for each of the seven keys in `TOPUP_LIMITS`, computed by `EntitlementService`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, RevisionedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import (
    CURRENCY_CHECK_SQL,
    CURRENCY_LENGTH,
    DEFAULT_CURRENCY,
    MAX_BILLING_REASON_LENGTH,
    MAX_LIMIT_VALUE,
    MAX_PLAN_CODE_LENGTH,
    MAX_PLAN_NAME_LENGTH,
    MAX_PLAN_PRICE,
    RESOURCE_LIMITS,
    TOPUP_LIMITS,
    LimitKey,
)
from app.db.models.enums import _enum_type
from app.db.models.invoice import MAX_IDEMPOTENCY_KEY_LENGTH

# Shown for a platform grant, which has no product to name.
PLATFORM_GRANT_NAME: Final = "Platform grant"


class TopupEntitlement(StrEnum):
    """The seven entitlements a top-up may raise - `TOPUP_LIMITS`, as a type.

    A native enum rather than a free string, so an unknown key is refused by
    the database as well as by the API: a product that raised a key nothing
    reads would be money taken for nothing.
    """

    PERIOD_MESSAGES = LimitKey.PERIOD_MESSAGES.value
    PERIOD_AI_TURNS = LimitKey.PERIOD_AI_TURNS.value
    PERIOD_CAMPAIGN_MESSAGES = LimitKey.PERIOD_CAMPAIGN_MESSAGES.value
    STORAGE_BYTES = LimitKey.STORAGE_BYTES.value
    WHATSAPP_NUMBERS = LimitKey.WHATSAPP_NUMBERS.value
    TEAM_MEMBERS = LimitKey.TEAM_MEMBERS.value
    KNOWLEDGE_DOCUMENTS = LimitKey.KNOWLEDGE_DOCUMENTS.value

    @property
    def limit_key(self) -> LimitKey:
        return LimitKey(self.value)

    @property
    def is_capacity(self) -> bool:
        """Whether this raises a capacity rather than a period allowance."""
        return self.limit_key in RESOURCE_LIMITS


# The two sets are one fact written twice; saying so at import is cheaper than
# finding out from a top-up that raises nothing.
if {member.limit_key for member in TopupEntitlement} != set(TOPUP_LIMITS):  # pragma: no cover
    raise RuntimeError("TopupEntitlement and TOPUP_LIMITS disagree about the top-up keys.")


class TopupScope(StrEnum):
    """Who may see, buy and be granted a product."""

    GLOBAL = "global"
    TENANT = "tenant"


class TopupValidity(StrEnum):
    """How long a granted top-up lasts. One rule in v1, named so it can grow.

    ``CURRENT_PERIOD_END``
        Until the end of the billing period it was bought in, captured when
        the checkout opened. No carry-over.
    """

    CURRENT_PERIOD_END = "current_period_end"


class TopupSource(StrEnum):
    """Where a held top-up came from - money, or an operator's decision."""

    PURCHASE = "purchase"
    PLATFORM_GRANT = "platform_grant"


class TopupStatus(StrEnum):
    """Where one purchase stands. Financial success and the grant are distinct.

    ``PENDING``
        Checkout opened; no money yet.
    ``PAID``
        Money settled and the allowance **not** granted - the period it was for
        had ended, or the subscription had stopped serving. Always paired with
        a `topup_paid_but_not_granted` incident.
    ``GRANTED``
        The allowance is live until `expires_at`.
    ``EXPIRED``
        Past `expires_at`. Recorded by the billing sweep; the limit arithmetic
        never waits for it, because it filters on the clock.
    ``CANCELLED``
        Abandoned, refunded before it was granted, or withdrawn by an operator
        after review. Contributes nothing.
    ``REFUND_REVIEW``
        Refunded after it was granted. **Still counts** - withdrawing an
        allowance somebody may already have used is an operator's decision,
        never an automatic one (ADR-113).
    """

    PENDING = "pending"
    PAID = "paid"
    GRANTED = "granted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUND_REVIEW = "refund_review"


# The statuses whose quantity counts toward the effective limit, while
# `granted_at <= now < expires_at`. Named once: the entitlement query, the
# summaries and the invariant sweep must all mean the same set.
ACTIVE_TOPUP_STATUSES: Final[frozenset[TopupStatus]] = frozenset(
    {TopupStatus.GRANTED, TopupStatus.REFUND_REVIEW}
)

TOPUP_TRANSITIONS: Final[dict[TopupStatus, frozenset[TopupStatus]]] = {
    TopupStatus.PENDING: frozenset({TopupStatus.PAID, TopupStatus.GRANTED, TopupStatus.CANCELLED}),
    TopupStatus.PAID: frozenset({TopupStatus.GRANTED, TopupStatus.CANCELLED}),
    TopupStatus.GRANTED: frozenset({TopupStatus.EXPIRED, TopupStatus.REFUND_REVIEW}),
    TopupStatus.REFUND_REVIEW: frozenset({TopupStatus.GRANTED, TopupStatus.CANCELLED}),
    TopupStatus.EXPIRED: frozenset(),
    TopupStatus.CANCELLED: frozenset(),
}


def topup_may_move(current: TopupStatus, target: TopupStatus) -> bool:
    """Whether a purchase may go from `current` to `target`."""
    return target in TOPUP_TRANSITIONS[current]


TOPUP_ENTITLEMENT_TYPE = _enum_type(TopupEntitlement, name="topup_entitlement")
TOPUP_SCOPE_TYPE = _enum_type(TopupScope, name="topup_scope")
TOPUP_VALIDITY_TYPE = _enum_type(TopupValidity, name="topup_validity")
TOPUP_SOURCE_TYPE = _enum_type(TopupSource, name="topup_source")
TOPUP_STATUS_TYPE = _enum_type(TopupStatus, name="topup_status")

_QUANTITY_CHECK = f"quantity > 0 AND quantity <= {MAX_LIMIT_VALUE}"


class TopupProduct(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """One add-on the platform sells: which entitlement, how much, for what."""

    __tablename__ = "topup_products"
    __table_args__ = (
        UniqueConstraint("code", name="uq_topup_products_code"),
        Index("ix_topup_products_tenant_id", "tenant_id"),
        Index("ix_topup_products_is_active", "is_active"),
        CheckConstraint(_QUANTITY_CHECK, name="quantity_positive"),
        CheckConstraint(f"price >= 0 AND price <= {MAX_PLAN_PRICE}", name="price_in_range"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        CheckConstraint("(scope = 'tenant') = (tenant_id IS NOT NULL)", name="scope_tenant"),
    )

    code: Mapped[str] = mapped_column(String(MAX_PLAN_CODE_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_PLAN_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entitlement_key: Mapped[TopupEntitlement] = mapped_column(
        TOPUP_ENTITLEMENT_TYPE, nullable=False
    )
    # BIGINT: a storage top-up is counted in bytes, and 25 GiB does not fit in
    # an INTEGER.
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH), nullable=False, default=DEFAULT_CURRENCY
    )
    scope: Mapped[TopupScope] = mapped_column(TOPUP_SCOPE_TYPE, nullable=False)
    # The one workspace a TENANT product is for. RESTRICT: a product somebody
    # bought is part of the commercial record (BILL-19).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Offered for new purchases. Deactivating changes nobody who already
    # bought it (ADR-113).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Listed in the customer catalogue and purchasable at checkout. False is a
    # draft: kept, editable, invisible to customers.
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validity_policy: Mapped[TopupValidity] = mapped_column(
        TOPUP_VALIDITY_TYPE, nullable=False, default=TopupValidity.CURRENT_PERIOD_END
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def visible_to(self, tenant_id: uuid.UUID) -> bool:
        """Whether a customer of `tenant_id` may see and buy this now."""
        if not (self.is_active and self.is_public):
            return False
        return self.scope is TopupScope.GLOBAL or self.tenant_id == tenant_id

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"TopupProduct(code={self.code!r}, entitlement={self.entitlement_key!r})"


class TopupPurchase(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """One top-up a workspace holds, bought or granted, frozen at creation."""

    __tablename__ = "topup_purchases"
    __table_args__ = (
        # One purchase per invoice: settling an invoice can only ever find one
        # grant to make, however many times its money is reported.
        UniqueConstraint("invoice_id", name="uq_topup_purchases_invoice_id"),
        # A retried checkout request is the same purchase, never a second one.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_topup_purchases_tenant_id_idempotency_key"
        ),
        # The effective-limit query: one workspace, one key, what is live.
        Index(
            "ix_topup_purchases_active",
            "tenant_id",
            "entitlement_key",
            "status",
            "expires_at",
        ),
        # The expiry sweep.
        Index("ix_topup_purchases_status_expires_at", "status", "expires_at"),
        Index("ix_topup_purchases_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_topup_purchases_topup_product_id", "topup_product_id"),
        CheckConstraint(_QUANTITY_CHECK, name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        CheckConstraint("billing_period_end > billing_period_start", name="period_ordered"),
        CheckConstraint("expires_at > billing_period_start", name="expiry_after_start"),
        CheckConstraint(
            "status NOT IN ('granted', 'expired', 'refund_review') OR granted_at IS NOT NULL",
            name="granted_has_moment",
        ),
        CheckConstraint("granted_at IS NULL OR expires_at > granted_at", name="expiry_after_grant"),
        # A grant is not a sale, and the ledger says so: no invoice, no
        # payment, no price, and a reason (spec: no fake provider payment).
        CheckConstraint(
            "source <> 'platform_grant' OR (invoice_id IS NULL AND payment_id IS NULL "
            "AND unit_price = 0 AND total_amount = 0 AND reason IS NOT NULL)",
            name="grant_is_not_a_sale",
        ),
        # A purchase is a sale: of a product, against an invoice.
        CheckConstraint(
            "source <> 'purchase' OR (invoice_id IS NOT NULL AND topup_product_id IS NOT NULL)",
            name="purchase_is_invoiced",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT (BILL-19): a purchase is financial history.
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topup_product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topup_products.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[TopupSource] = mapped_column(TOPUP_SOURCE_TYPE, nullable=False)
    # The snapshot. Everything from here to `expires_at` is frozen by a trigger.
    product_code: Mapped[str | None] = mapped_column(String(MAX_PLAN_CODE_LENGTH), nullable=True)
    product_name: Mapped[str] = mapped_column(String(MAX_PLAN_NAME_LENGTH), nullable=False)
    entitlement_key: Mapped[TopupEntitlement] = mapped_column(
        TOPUP_ENTITLEMENT_TYPE, nullable=False
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH), nullable=False, default=DEFAULT_CURRENCY
    )
    billing_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    billing_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[TopupStatus] = mapped_column(TOPUP_STATUS_TYPE, nullable=False)
    # The money behind a purchase. RESTRICT: the ledger outlives any attempt to
    # tidy it away.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_LENGTH), nullable=True
    )
    # Why an operator granted or decided something. Required for a grant.
    reason: Mapped[str | None] = mapped_column(String(MAX_BILLING_REASON_LENGTH), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    @property
    def limit_key(self) -> LimitKey:
        return self.entitlement_key.limit_key

    def is_active_at(self, moment: datetime) -> bool:
        """Whether this contributes to the effective limit at `moment`."""
        return (
            self.status in ACTIVE_TOPUP_STATUSES
            and self.granted_at is not None
            and self.granted_at <= moment < self.expires_at
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return (
            f"TopupPurchase(tenant_id={self.tenant_id!r}, key={self.entitlement_key!r}, "
            f"status={self.status!r})"
        )


# What a customer was shown is what they hold (spec: immutable top-up checkout).
# The money links may be written once - a payment is created after its
# purchase - and never re-pointed. Restated verbatim by migration 0072.
TOPUP_SNAPSHOT_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION topup_purchases_refuse_snapshot_change() RETURNS trigger AS $$
    BEGIN
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.topup_product_id IS DISTINCT FROM OLD.topup_product_id
           OR NEW.source IS DISTINCT FROM OLD.source
           OR NEW.product_code IS DISTINCT FROM OLD.product_code
           OR NEW.product_name IS DISTINCT FROM OLD.product_name
           OR NEW.entitlement_key IS DISTINCT FROM OLD.entitlement_key
           OR NEW.quantity IS DISTINCT FROM OLD.quantity
           OR NEW.unit_price IS DISTINCT FROM OLD.unit_price
           OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.billing_period_start IS DISTINCT FROM OLD.billing_period_start
           OR NEW.billing_period_end IS DISTINCT FROM OLD.billing_period_end
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR (OLD.invoice_id IS NOT NULL AND NEW.invoice_id IS DISTINCT FROM OLD.invoice_id)
           OR (OLD.payment_id IS NOT NULL AND NEW.payment_id IS DISTINCT FROM OLD.payment_id)
        THEN
            RAISE EXCEPTION 'a top-up purchase keeps the terms it was bought at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
TOPUP_SNAPSHOT_TRIGGER_SQL: Final = (
    "CREATE TRIGGER topup_purchases_snapshot_immutable BEFORE UPDATE ON topup_purchases "
    "FOR EACH ROW EXECUTE FUNCTION topup_purchases_refuse_snapshot_change()"
)
# A tenant product is bought or granted by its own workspace only.
TOPUP_SCOPE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION topup_purchases_refuse_foreign_product() RETURNS trigger AS $$
    BEGIN
        IF NEW.topup_product_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM topup_products p
             WHERE p.id = NEW.topup_product_id
               AND p.tenant_id IS NOT NULL
               AND p.tenant_id <> NEW.tenant_id
        ) THEN
            RAISE EXCEPTION 'topup_not_available_for_workspace'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
TOPUP_SCOPE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER topup_purchases_product_scope BEFORE INSERT OR UPDATE OF "
    "tenant_id, topup_product_id ON topup_purchases "
    "FOR EACH ROW EXECUTE FUNCTION topup_purchases_refuse_foreign_product()"
)

for _statement in (
    TOPUP_SNAPSHOT_FUNCTION_SQL,
    TOPUP_SNAPSHOT_TRIGGER_SQL,
    TOPUP_SCOPE_FUNCTION_SQL,
    TOPUP_SCOPE_TRIGGER_SQL,
):
    event.listen(TopupPurchase.__table__, "after_create", DDL(_statement))  # type: ignore[no-untyped-call]


__all__ = [
    "ACTIVE_TOPUP_STATUSES",
    "PLATFORM_GRANT_NAME",
    "TOPUP_TRANSITIONS",
    "TopupEntitlement",
    "TopupProduct",
    "TopupPurchase",
    "TopupScope",
    "TopupSource",
    "TopupStatus",
    "TopupValidity",
    "topup_may_move",
]
