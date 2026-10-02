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
    ForeignKeyConstraint,
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
    RETIRED_LIMIT_KEY,
    TOPUP_LIMITS,
    LimitKey,
)
from app.db.models.channel import CHANNEL_TYPE, Channel
from app.db.models.enums import _enum_type
from app.db.models.invoice import MAX_IDEMPOTENCY_KEY_LENGTH

# Shown for a platform grant, which has no product to name.
PLATFORM_GRANT_NAME: Final = "Platform grant"


class TopupEntitlement(StrEnum):
    """The seven entitlements a top-up may raise - `TOPUP_LIMITS`, as a type.

    A native enum rather than a free string, so an unknown key is refused by
    the database as well as by the API: a product that raised a key nothing
    reads would be money taken for nothing.

    `WHATSAPP_NUMBERS` is retired (ENT-05, ADR-131). PostgreSQL cannot drop an
    enum label, so the member stays for the type to match the database, and a
    trigger refuses it on every new product and purchase. A purchase written
    before the change still counts, toward `channel_connections`.
    """

    PERIOD_MESSAGES = LimitKey.PERIOD_MESSAGES.value
    PERIOD_AI_TURNS = LimitKey.PERIOD_AI_TURNS.value
    PERIOD_CAMPAIGN_MESSAGES = LimitKey.PERIOD_CAMPAIGN_MESSAGES.value
    STORAGE_BYTES = LimitKey.STORAGE_BYTES.value
    WHATSAPP_NUMBERS = RETIRED_LIMIT_KEY
    TEAM_MEMBERS = LimitKey.TEAM_MEMBERS.value
    KNOWLEDGE_DOCUMENTS = LimitKey.KNOWLEDGE_DOCUMENTS.value
    # Appended, as `ALTER TYPE ... ADD VALUE` appends (0093).
    CHANNEL_CONNECTIONS = LimitKey.CHANNEL_CONNECTIONS.value

    @property
    def limit_key(self) -> LimitKey:
        if self is TopupEntitlement.WHATSAPP_NUMBERS:
            # A pre-change purchase raised the number limit, which is now the
            # channel capacity: the slots it bought are slots (ENT-05).
            return LimitKey.CHANNEL_CONNECTIONS
        return LimitKey(self.value)

    @property
    def is_retired(self) -> bool:
        """Whether no new product or purchase may name this key (ENT-05)."""
        return self is TopupEntitlement.WHATSAPP_NUMBERS

    @property
    def is_capacity(self) -> bool:
        """Whether this raises a capacity rather than a period allowance."""
        return self.limit_key in RESOURCE_LIMITS


#: The keys a product may still be created for: every member but the retired.
SELLABLE_TOPUP_ENTITLEMENTS: Final[tuple[TopupEntitlement, ...]] = tuple(
    member for member in TopupEntitlement if not member.is_retired
)

# The two sets are one fact written twice; saying so at import is cheaper than
# finding out from a top-up that raises nothing.
if {member.limit_key for member in SELLABLE_TOPUP_ENTITLEMENTS} != set(
    TOPUP_LIMITS
):  # pragma: no cover
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
_TYPED_CHECK = "channel_type IS NULL OR entitlement_key = 'channel_connections'"


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
        # Only channel capacity can be typed (ENT-11): a slot usable by one
        # channel type. Every other key is untyped.
        CheckConstraint(_TYPED_CHECK, name="channel_type_for_channel_capacity"),
    )

    code: Mapped[str] = mapped_column(String(MAX_PLAN_CODE_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_PLAN_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    entitlement_key: Mapped[TopupEntitlement] = mapped_column(
        TOPUP_ENTITLEMENT_TYPE, nullable=False
    )
    # A `channel_connections` slot usable only by connections of this channel
    # type, or null for a general slot any allowed type may use (ENT-11). A
    # typed product never opens a channel type: one the workspace's plan does
    # not allow is not listed, not sold and not granted to it (ENT-12).
    channel_type: Mapped[Channel | None] = mapped_column(CHANNEL_TYPE, nullable=True)
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
        """Whether a customer of `tenant_id` may see and buy this now.

        Scope and state only. Plan eligibility (ENT-13) and the plan's allowed
        channel types (ENT-12) depend on the workspace's plan, which
        `TopupService` resolves and checks beside this.
        """
        if not (self.is_active and self.is_public):
            return False
        return self.scope is TopupScope.GLOBAL or self.tenant_id == tenant_id

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"TopupProduct(code={self.code!r}, entitlement={self.entitlement_key!r})"


class TopupProductPlan(Base):
    """One plan a top-up product is offered to (ENT-13).

    A product with no rows here is offered to every plan - today's behaviour.
    A product with rows is seen, bought and granted only by workspaces whose
    pinned plan is one of them. Platform staff decide, through the product API.
    Cascades both ways: eligibility is a property of the pair and means nothing
    once either is gone, and neither a sold product nor a held plan can be
    deleted anyway.
    """

    __tablename__ = "topup_product_plans"
    __table_args__ = (Index("ix_topup_product_plans_plan_id", "plan_id"),)

    topup_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topup_products.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopupPurchase(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """One top-up a workspace holds, bought or granted, frozen at creation."""

    __tablename__ = "topup_purchases"
    __table_args__ = (
        # One purchase per invoice: settling an invoice can only ever find one
        # grant to make, however many times its money is reported.
        UniqueConstraint("invoice_id", name="uq_topup_purchases_invoice_id"),
        # A purchase is paid by its own workspace's invoice and payment
        # (DB-004): the audit bound one workspace's top-up to another's
        # invoice with plain SQL.
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_topup_purchases_tenant_invoice",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "payment_id"],
            ["payments.tenant_id", "payments.id"],
            name="fk_topup_purchases_tenant_payment",
            ondelete="RESTRICT",
        ),
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
        CheckConstraint(_TYPED_CHECK, name="channel_type_for_channel_capacity"),
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
    # Frozen with the rest (ENT-10): a typed slot stays the type it was bought
    # or granted for, whatever the product becomes. Null is a general slot.
    channel_type: Mapped[Channel | None] = mapped_column(CHANNEL_TYPE, nullable=True)
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
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
           OR NEW.channel_type IS DISTINCT FROM OLD.channel_type
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
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
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
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
TOPUP_SCOPE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER topup_purchases_product_scope BEFORE INSERT OR UPDATE OF "
    "tenant_id, topup_product_id ON topup_purchases "
    "FOR EACH ROW EXECUTE FUNCTION topup_purchases_refuse_foreign_product()"
)

# **Unpaid is never granted** (ADR-113 §6, DB-005). A purchased allowance
# becomes live only once its invoice is paid; a platform grant carries no
# invoice and is untouched. Deferred to commit, because settlement marks the
# invoice paid and grants the purchase in the same transaction in whichever
# order its flushes run; checked when a purchase *becomes* granted, re-reading
# both rows as the transaction leaves them.
TOPUP_GRANT_PAID_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION topup_purchases_refuse_unpaid_grant() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM topup_purchases t
              LEFT JOIN invoices i ON i.id = t.invoice_id
             WHERE t.id = NEW.id
               AND t.source::text = 'purchase'
               AND t.status::text = 'granted'
               AND (i.id IS NULL OR i.status::text <> 'paid')
        ) THEN
            RAISE EXCEPTION 'a purchased top-up is granted only once its invoice is paid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
TOPUP_GRANT_PAID_TRIGGER_SQL: Final = (
    "CREATE CONSTRAINT TRIGGER topup_purchases_grant_paid "
    "AFTER INSERT OR UPDATE OF status ON topup_purchases "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "WHEN (NEW.status::text = 'granted' AND NEW.source::text = 'purchase') "
    "EXECUTE FUNCTION topup_purchases_refuse_unpaid_grant()"
)

# **`whatsapp_numbers` is retired** (ENT-05, ADR-131). The label stays in the
# type because PostgreSQL cannot drop one; this refuses it on every new product
# and purchase, and on a product re-pointed at it, so no writer - the API, a
# fixture, SQL - can sell or grant the old key again. Restated verbatim by 0093.
TOPUP_RETIRED_KEY_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION topup_refuse_retired_key() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF NEW.entitlement_key::text = 'whatsapp_numbers' THEN
            RAISE EXCEPTION 'whatsapp_numbers is retired; sell channel_connections instead'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
TOPUP_PRODUCTS_RETIRED_KEY_TRIGGER_SQL: Final = (
    "CREATE TRIGGER topup_products_retired_key BEFORE INSERT OR UPDATE OF entitlement_key "
    "ON topup_products FOR EACH ROW EXECUTE FUNCTION topup_refuse_retired_key()"
)
TOPUP_PURCHASES_RETIRED_KEY_TRIGGER_SQL: Final = (
    "CREATE TRIGGER topup_purchases_retired_key BEFORE INSERT ON topup_purchases "
    "FOR EACH ROW EXECUTE FUNCTION topup_refuse_retired_key()"
)

for _statement in (
    TOPUP_SNAPSHOT_FUNCTION_SQL,
    TOPUP_SNAPSHOT_TRIGGER_SQL,
    TOPUP_SCOPE_FUNCTION_SQL,
    TOPUP_SCOPE_TRIGGER_SQL,
    TOPUP_GRANT_PAID_FUNCTION_SQL,
    TOPUP_GRANT_PAID_TRIGGER_SQL,
    TOPUP_RETIRED_KEY_FUNCTION_SQL,
    TOPUP_PURCHASES_RETIRED_KEY_TRIGGER_SQL,
):
    event.listen(TopupPurchase.__table__, "after_create", DDL(_statement))  # type: ignore[no-untyped-call]
# The function exists by the time the products trigger needs it only if the
# purchases table was built first; restating it here makes the order moot.
event.listen(TopupProduct.__table__, "after_create", DDL(TOPUP_RETIRED_KEY_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(TopupProduct.__table__, "after_create", DDL(TOPUP_PRODUCTS_RETIRED_KEY_TRIGGER_SQL))  # type: ignore[no-untyped-call]


__all__ = [
    "ACTIVE_TOPUP_STATUSES",
    "PLATFORM_GRANT_NAME",
    "SELLABLE_TOPUP_ENTITLEMENTS",
    "TOPUP_TRANSITIONS",
    "TopupEntitlement",
    "TopupProduct",
    "TopupProductPlan",
    "TopupPurchase",
    "TopupScope",
    "TopupSource",
    "TopupStatus",
    "TopupValidity",
    "topup_may_move",
]
