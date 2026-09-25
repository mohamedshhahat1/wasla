"""Tenant custom plans and one-time top-ups.

Revision ID: 0072
Revises: 0071

**Plan scope (ADR-113).** `plans.scope` is `public`, `private` or `tenant`, and
`plans.tenant_id` names the one workspace a `tenant` plan belongs to. Existing
plans are backfilled from `is_public` - public stays public, everything else
becomes private - so no existing plan becomes a custom plan and no subscriber
moves. Two CHECKs tie the columns together (`tenant` exactly when a tenant is
named; `public` exactly when `is_public`), and triggers make the binding a
property of the ledger rather than of the code that writes it today:

* `subscriptions` and `invoices` refuse to name another workspace's custom plan
  (its plan, pinned version, scheduled version, or invoiced version);
* a plan's owning tenant never changes;
* a plan inserted without a scope (raw SQL, a seed) takes the one its
  `is_public` implies;
* `payments` refuses an automatic (MIT) attempt against a top-up invoice.

**Top-ups.** `topup_products` is the catalogue and `topup_purchases` the ledger
of what workspaces hold, bought or granted by an operator. A purchase freezes
what the customer was shown, and a trigger refuses changes to that snapshot; a
second trigger refuses a tenant product being bought by, or granted to, any
other workspace. Both tables use RESTRICT foreign keys to their tenant, like the
rest of the financial ledger (BILL-19).

Enum labels (`invoice_purpose.topup`, six incident kinds, the new audit actions)
are added last, outside the transaction, as `ADD VALUE` requires. PostgreSQL
cannot drop an enum label, so the downgrade leaves them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

PLAN_SCOPES = ("public", "private", "tenant")
TOPUP_ENTITLEMENTS = (
    "period_messages",
    "period_ai_turns",
    "period_campaign_messages",
    "storage_bytes",
    "whatsapp_numbers",
    "team_members",
    "knowledge_documents",
)
TOPUP_SCOPES = ("global", "tenant")
TOPUP_VALIDITIES = ("current_period_end",)
TOPUP_SOURCES = ("purchase", "platform_grant")
TOPUP_STATUSES = ("pending", "paid", "granted", "expired", "cancelled", "refund_review")

NEW_INCIDENT_KINDS = (
    "topup_paid_but_not_granted",
    "topup_duplicate_payment",
    "topup_refund_after_consumption",
    "topup_entitlement_reversal_blocked",
    "topup_unknown_callback",
    "custom_plan_scope_mismatch",
)
NEW_ACTIONS = (
    "billing_custom_plan_created",
    "billing_custom_plan_version_created",
    "billing_custom_plan_assigned",
    "billing_custom_plan_assignment_scheduled",
    "billing_topup_created",
    "billing_topup_updated",
    "billing_topup_activated",
    "billing_topup_deactivated",
    "billing_topup_deleted",
    "billing_topup_checkout_created",
    "billing_topup_payment_settled",
    "billing_topup_granted",
    "billing_topup_expired",
    "billing_topup_cancelled",
    "billing_topup_platform_granted",
    "billing_topup_refund_reviewed",
)

MAX_LIMIT = 10**15
MAX_PRICE = "1000000.00"

# Restated rather than imported, so this migration keeps meaning what it meant
# when the models move on.
CUSTOM_PLAN_SCOPE_FUNCTION = """
CREATE OR REPLACE FUNCTION billing_refuse_foreign_custom_plan() RETURNS trigger AS $$
DECLARE
    foreign_plan uuid;
BEGIN
    IF TG_TABLE_NAME = 'subscriptions' THEN
        SELECT p.id INTO foreign_plan FROM plans p
         WHERE p.tenant_id IS NOT NULL
           AND p.tenant_id <> NEW.tenant_id
           AND (p.id = NEW.plan_id
                OR p.id IN (SELECT v.plan_id FROM plan_versions v
                             WHERE v.id = NEW.plan_version_id
                                OR v.id = NEW.scheduled_plan_version_id))
         LIMIT 1;
    ELSE
        SELECT p.id INTO foreign_plan FROM plans p
          JOIN plan_versions v ON v.plan_id = p.id
         WHERE v.id = NEW.plan_version_id
           AND p.tenant_id IS NOT NULL
           AND p.tenant_id <> NEW.tenant_id
         LIMIT 1;
    END IF;
    IF foreign_plan IS NOT NULL THEN
        RAISE EXCEPTION 'custom_plan_not_available_for_workspace'
            USING ERRCODE = 'integrity_constraint_violation',
                  DETAIL = 'The plan belongs to another workspace.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
PLAN_DERIVE_SCOPE_FUNCTION = """
CREATE OR REPLACE FUNCTION plans_derive_scope() RETURNS trigger AS $$
BEGIN
    IF NEW.scope IS NULL THEN
        NEW.scope := CASE WHEN NEW.is_public THEN 'public' ELSE 'private' END;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
PLAN_TENANT_FUNCTION = """
CREATE OR REPLACE FUNCTION plans_refuse_tenant_change() RETURNS trigger AS $$
BEGIN
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION 'a custom plan belongs to one workspace for ever'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
NO_AUTOMATIC_TOPUP_FUNCTION = """
CREATE OR REPLACE FUNCTION payments_refuse_automatic_topup() RETURNS trigger AS $$
BEGIN
    IF NEW.is_automatic AND EXISTS (
        SELECT 1 FROM invoices i
         WHERE i.id = NEW.invoice_id AND i.purpose::text = 'topup'
    ) THEN
        RAISE EXCEPTION 'topup invoices are never collected automatically'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
TOPUP_SNAPSHOT_FUNCTION = """
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
TOPUP_SCOPE_FUNCTION = """
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

# (trigger, table, timing, function)
TRIGGERS = (
    (
        "plans_derive_scope",
        "plans",
        "BEFORE INSERT",
        "plans_derive_scope",
    ),
    (
        "plans_tenant_immutable",
        "plans",
        "BEFORE UPDATE OF tenant_id, scope",
        "plans_refuse_tenant_change",
    ),
    (
        "subscriptions_custom_plan_scope",
        "subscriptions",
        "BEFORE INSERT OR UPDATE OF tenant_id, plan_id, plan_version_id, "
        "scheduled_plan_version_id",
        "billing_refuse_foreign_custom_plan",
    ),
    (
        "invoices_custom_plan_scope",
        "invoices",
        "BEFORE INSERT OR UPDATE OF tenant_id, plan_version_id",
        "billing_refuse_foreign_custom_plan",
    ),
    (
        "payments_no_automatic_topup",
        "payments",
        "BEFORE INSERT OR UPDATE OF is_automatic, invoice_id",
        "payments_refuse_automatic_topup",
    ),
    (
        "topup_purchases_snapshot_immutable",
        "topup_purchases",
        "BEFORE UPDATE",
        "topup_purchases_refuse_snapshot_change",
    ),
    (
        "topup_purchases_product_scope",
        "topup_purchases",
        "BEFORE INSERT OR UPDATE OF tenant_id, topup_product_id",
        "topup_purchases_refuse_foreign_product",
    ),
)
FUNCTIONS = (
    "plans_derive_scope",
    "plans_refuse_tenant_change",
    "billing_refuse_foreign_custom_plan",
    "payments_refuse_automatic_topup",
    "topup_purchases_refuse_snapshot_change",
    "topup_purchases_refuse_foreign_product",
)


def _enum(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    ]


def _fk(table: str, column: str, target: str, *, ondelete: str, nullable: bool = True) -> sa.Column:
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(f"{target}.id", ondelete=ondelete, name=f"fk_{table}_{column}_{target}"),
        nullable=nullable,
    )


def upgrade() -> None:
    bind = op.get_bind()
    enums = {
        "plan_scope": _enum("plan_scope", PLAN_SCOPES),
        "topup_entitlement": _enum("topup_entitlement", TOPUP_ENTITLEMENTS),
        "topup_scope": _enum("topup_scope", TOPUP_SCOPES),
        "topup_validity": _enum("topup_validity", TOPUP_VALIDITIES),
        "topup_source": _enum("topup_source", TOPUP_SOURCES),
        "topup_status": _enum("topup_status", TOPUP_STATUSES),
    }
    for enum in enums.values():
        enum.create(bind, checkfirst=True)

    # ------------------------------------------------------------ plan scope
    # Added with a default to fill every existing row, then backfilled from
    # `is_public`, then the default dropped: the model has none, and a new plan
    # always states its scope.
    op.add_column(
        "plans",
        sa.Column("scope", enums["plan_scope"], nullable=False, server_default="public"),
    )
    op.execute("UPDATE plans SET scope = 'private' WHERE is_public IS FALSE")
    op.alter_column("plans", "scope", server_default=None)
    op.add_column("plans", _fk("plans", "tenant_id", "tenants", ondelete="RESTRICT"))
    op.create_index("ix_plans_tenant_id", "plans", ["tenant_id"])
    op.create_check_constraint(
        op.f("ck_plans_scope_tenant"), "plans", "(scope = 'tenant') = (tenant_id IS NOT NULL)"
    )
    op.create_check_constraint(
        op.f("ck_plans_scope_visibility"), "plans", "(scope = 'public') = is_public"
    )

    # ----------------------------------------------------------- top-up catalogue
    op.create_table(
        "topup_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entitlement_key", enums["topup_entitlement"], nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("scope", enums["topup_scope"], nullable=False),
        _fk("topup_products", "tenant_id", "tenants", ondelete="RESTRICT"),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("validity_policy", enums["topup_validity"], nullable=False),
        _fk("topup_products", "created_by", "users", ondelete="SET NULL"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_topup_products"),
        sa.UniqueConstraint("code", name="uq_topup_products_code"),
        sa.CheckConstraint(
            f"quantity > 0 AND quantity <= {MAX_LIMIT}",
            name=op.f("ck_topup_products_quantity_positive"),
        ),
        sa.CheckConstraint(
            f"price >= 0 AND price <= {MAX_PRICE}", name=op.f("ck_topup_products_price_in_range")
        ),
        sa.CheckConstraint("currency = 'EGP'", name=op.f("ck_topup_products_currency_supported")),
        sa.CheckConstraint(
            "(scope = 'tenant') = (tenant_id IS NOT NULL)",
            name=op.f("ck_topup_products_scope_tenant"),
        ),
    )
    op.create_index("ix_topup_products_tenant_id", "topup_products", ["tenant_id"])
    op.create_index("ix_topup_products_is_active", "topup_products", ["is_active"])

    # ------------------------------------------------------------ top-up ledger
    op.create_table(
        "topup_purchases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        _fk("topup_purchases", "tenant_id", "tenants", ondelete="RESTRICT", nullable=False),
        _fk("topup_purchases", "topup_product_id", "topup_products", ondelete="RESTRICT"),
        _fk("topup_purchases", "subscription_id", "subscriptions", ondelete="SET NULL"),
        sa.Column("source", enums["topup_source"], nullable=False),
        sa.Column("product_code", sa.String(50), nullable=True),
        sa.Column("product_name", sa.String(100), nullable=False),
        sa.Column("entitlement_key", enums["topup_entitlement"], nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("billing_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("billing_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", enums["topup_status"], nullable=False),
        _fk("topup_purchases", "invoice_id", "invoices", ondelete="RESTRICT"),
        _fk("topup_purchases", "payment_id", "payments", ondelete="RESTRICT"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        _fk("topup_purchases", "actor_id", "users", ondelete="SET NULL"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_topup_purchases"),
        sa.UniqueConstraint("invoice_id", name="uq_topup_purchases_invoice_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_topup_purchases_tenant_id_idempotency_key",
        ),
        sa.CheckConstraint(
            f"quantity > 0 AND quantity <= {MAX_LIMIT}",
            name=op.f("ck_topup_purchases_quantity_positive"),
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_topup_purchases_unit_price_non_negative")
        ),
        sa.CheckConstraint(
            "total_amount >= 0", name=op.f("ck_topup_purchases_total_amount_non_negative")
        ),
        sa.CheckConstraint("currency = 'EGP'", name=op.f("ck_topup_purchases_currency_supported")),
        sa.CheckConstraint(
            "billing_period_end > billing_period_start",
            name=op.f("ck_topup_purchases_period_ordered"),
        ),
        sa.CheckConstraint(
            "expires_at > billing_period_start", name=op.f("ck_topup_purchases_expiry_after_start")
        ),
        sa.CheckConstraint(
            "status NOT IN ('granted', 'expired', 'refund_review') OR granted_at IS NOT NULL",
            name=op.f("ck_topup_purchases_granted_has_moment"),
        ),
        sa.CheckConstraint(
            "granted_at IS NULL OR expires_at > granted_at",
            name=op.f("ck_topup_purchases_expiry_after_grant"),
        ),
        sa.CheckConstraint(
            "source <> 'platform_grant' OR (invoice_id IS NULL AND payment_id IS NULL "
            "AND unit_price = 0 AND total_amount = 0 AND reason IS NOT NULL)",
            name=op.f("ck_topup_purchases_grant_is_not_a_sale"),
        ),
        sa.CheckConstraint(
            "source <> 'purchase' OR (invoice_id IS NOT NULL AND topup_product_id IS NOT NULL)",
            name=op.f("ck_topup_purchases_purchase_is_invoiced"),
        ),
    )
    op.create_index(
        "ix_topup_purchases_active",
        "topup_purchases",
        ["tenant_id", "entitlement_key", "status", "expires_at"],
    )
    op.create_index(
        "ix_topup_purchases_status_expires_at", "topup_purchases", ["status", "expires_at"]
    )
    op.create_index(
        "ix_topup_purchases_tenant_id_created_at", "topup_purchases", ["tenant_id", "created_at"]
    )
    op.create_index("ix_topup_purchases_topup_product_id", "topup_purchases", ["topup_product_id"])

    # -------------------------------------------------------------- triggers
    for function in (
        PLAN_DERIVE_SCOPE_FUNCTION,
        PLAN_TENANT_FUNCTION,
        CUSTOM_PLAN_SCOPE_FUNCTION,
        NO_AUTOMATIC_TOPUP_FUNCTION,
        TOPUP_SNAPSHOT_FUNCTION,
        TOPUP_SCOPE_FUNCTION,
    ):
        op.execute(function)
    for name, table, timing, function_name in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {name} {timing} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )

    # Last, and outside the transaction: `ADD VALUE` cannot run inside one.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE invoice_purpose ADD VALUE IF NOT EXISTS 'topup'")
        for value in NEW_INCIDENT_KINDS:
            op.execute(f"ALTER TYPE billing_incident_kind ADD VALUE IF NOT EXISTS '{value}'")
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Return to the 0071 schema.

    Refused while any top-up invoice exists: `invoice_purpose` keeps its
    `topup` label (PostgreSQL cannot drop one), and a 0071 application reading
    such an invoice would not know what it is. Custom plans become private
    plans - their subscribers keep them - and the top-up ledger is dropped.
    """
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM invoices WHERE purpose::text = 'topup') THEN
                RAISE EXCEPTION 'top-up invoices exist; 0072 cannot be downgraded safely';
            END IF;
        END $$;
        """)
    for name, table, _timing, _function in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    op.drop_table("topup_purchases")
    op.drop_table("topup_products")

    op.drop_constraint(op.f("ck_plans_scope_visibility"), "plans", type_="check")
    op.drop_constraint(op.f("ck_plans_scope_tenant"), "plans", type_="check")
    op.drop_index("ix_plans_tenant_id", table_name="plans")
    op.drop_column("plans", "tenant_id")
    op.drop_column("plans", "scope")

    bind = op.get_bind()
    for name in (
        "topup_status",
        "topup_source",
        "topup_validity",
        "topup_scope",
        "topup_entitlement",
        "plan_scope",
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
