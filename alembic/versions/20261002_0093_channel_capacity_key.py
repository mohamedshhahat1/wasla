"""Channel capacity: the `channel_connections` key, allowed channel types, typed top-ups.

Revision ID: 0093
Revises: 0092

ADR-131 (ENT-05, ENT-07, ENT-09, ENT-11, ENT-13).

1. **`topup_entitlement` gains `channel_connections`** - appended in an autocommit
   block first, because the constraints below name it. `whatsapp_numbers` stays
   as a label (PostgreSQL cannot drop one) and is refused on every new product
   and purchase by `topup_refuse_retired_key`. Existing products naming it become
   `channel_connections` slots typed `whatsapp` - exactly what they sold. Existing
   purchases are frozen by their snapshot trigger and are left as they are; the
   entitlement reader counts them as typed WhatsApp slots.
2. **`plan_versions.allowed_channel_types`** (and the `plans` mirror) - nullable.
   No stored version is rewritten: the immutability trigger is never disabled.
   A version published from now on must state its types and may not carry
   `whatsapp_numbers` (`plan_versions_entitlement_terms`, BEFORE INSERT); one
   published before keeps NULL, read as WhatsApp alone, and its number limit is
   read as its channel capacity (choice A).
3. **`topup_products.channel_type`, `topup_purchases.channel_type`** - a slot only
   one channel type may use (ENT-11), frozen in the purchase snapshot.
   **`topup_product_plans`** - the plans a product is offered to (ENT-13);
   none means every plan, today's behaviour.
4. **`channel_connections.disabled_reason / disabled_at / disabled_by`** - why
   and by whom a connection was last disabled (ENT-07, ENT-14).

**Online.** Every column is nullable with no default (metadata-only `ADD
COLUMN`); the CHECKs are added `NOT VALID` and validated; the tables touched are
small catalogue and connection tables. `lock_timeout` is bounded.

**Downgrade refuses** while any row uses what the pre-0093 schema cannot
represent: a version stating channel types, a channel purchase or typed product
other than a converted WhatsApp one, plan eligibility, a recorded disable, or
an audit entry of a neutral connection action. Converted WhatsApp products are
turned back into `whatsapp_numbers`. The enum labels stay.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

PLAN_VERSION_ENTITLEMENT_TERMS_FUNCTION = """
    CREATE OR REPLACE FUNCTION plan_versions_entitlement_terms() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    DECLARE
        label text;
    BEGIN
        IF NEW.allowed_channel_types IS NULL THEN
            RAISE EXCEPTION 'a plan version states the channel types it allows'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.limits ? 'whatsapp_numbers' THEN
            RAISE EXCEPTION 'whatsapp_numbers is retired; publish channel_connections instead'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF array_position(NEW.allowed_channel_types, NULL) IS NOT NULL
           OR cardinality(NEW.allowed_channel_types)
              <> (SELECT count(DISTINCT element) FROM unnest(NEW.allowed_channel_types) element)
        THEN
            RAISE EXCEPTION 'each allowed channel type is named once'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        FOREACH label IN ARRAY NEW.allowed_channel_types LOOP
            IF NOT label = ANY (enum_range(NULL::channel_kind)::text[]) THEN
                RAISE EXCEPTION 'unknown channel type in a plan version: %', label
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END LOOP;
        RETURN NEW;
    END;
    $$
    """
PLAN_VERSION_ENTITLEMENT_TERMS_TRIGGER = (
    "CREATE TRIGGER plan_versions_entitlement_terms BEFORE INSERT ON plan_versions "
    "FOR EACH ROW EXECUTE FUNCTION plan_versions_entitlement_terms()"
)

# The 0072 snapshot function, with the channel type frozen beside the rest.
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
# The 0072 function, restored on downgrade.
TOPUP_SNAPSHOT_FUNCTION_0072 = TOPUP_SNAPSHOT_FUNCTION.replace(
    "           OR NEW.channel_type IS DISTINCT FROM OLD.channel_type\n", ""
)

TOPUP_RETIRED_KEY_FUNCTION = """
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
TOPUP_PRODUCTS_RETIRED_KEY_TRIGGER = (
    "CREATE TRIGGER topup_products_retired_key BEFORE INSERT OR UPDATE OF entitlement_key "
    "ON topup_products FOR EACH ROW EXECUTE FUNCTION topup_refuse_retired_key()"
)
TOPUP_PURCHASES_RETIRED_KEY_TRIGGER = (
    "CREATE TRIGGER topup_purchases_retired_key BEFORE INSERT ON topup_purchases "
    "FOR EACH ROW EXECUTE FUNCTION topup_refuse_retired_key()"
)

TYPED_CHECK = "channel_type IS NULL OR entitlement_key = 'channel_connections'"
TYPED_CONSTRAINT = "ck_{table}_channel_type_for_channel_capacity"

DISABLED_REASONS = ("manual", "capacity_reduction", "capacity_reduction_automatic")

# Appended in this order, as the model's `AUDIT_ACTION_DATABASE_ORDER` lists them.
AUDIT_ACTIONS = (
    "channel_connection_connected",
    "channel_connection_enabled",
    "channel_connection_disabled",
    "channel_connection_released",
)

DOWNGRADE_PRECHECKS: tuple[tuple[str, str], ...] = (
    (
        "plan versions stating channel types",
        "SELECT count(*) FROM plan_versions WHERE allowed_channel_types IS NOT NULL",
    ),
    (
        "channel top-up purchases",
        "SELECT count(*) FROM topup_purchases"
        " WHERE entitlement_key::text = 'channel_connections' OR channel_type IS NOT NULL",
    ),
    (
        "channel top-up products other than a converted WhatsApp one",
        "SELECT count(*) FROM topup_products WHERE entitlement_key::text = 'channel_connections'"
        " AND channel_type IS DISTINCT FROM 'whatsapp'",
    ),
    ("plan-eligible top-up products", "SELECT count(*) FROM topup_product_plans"),
    (
        "connections with a recorded disable",
        "SELECT count(*) FROM channel_connections"
        " WHERE disabled_reason IS NOT NULL OR disabled_at IS NOT NULL",
    ),
    (
        "audit entries of a neutral connection action",
        "SELECT count(*) FROM audit_logs WHERE action::text IN ("
        "'channel_connection_connected', 'channel_connection_enabled',"
        " 'channel_connection_disabled', 'channel_connection_released')",
    ),
)


def _refuse() -> None:
    connection = op.get_bind()
    found = []
    for label, query in DOWNGRADE_PRECHECKS:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            "Rows use channel capacity the pre-0093 schema cannot represent "
            "(docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE topup_entitlement ADD VALUE IF NOT EXISTS 'channel_connections'")
        for action in AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    channel = postgresql.ENUM(name="channel_kind", create_type=False)
    disabled = postgresql.ENUM(*DISABLED_REASONS, name="connection_disabled_reason")
    disabled.create(op.get_bind(), checkfirst=True)

    # 2. Allowed channel types: nullable, and required on new versions.
    for table in ("plans", "plan_versions"):
        op.add_column(
            table,
            sa.Column("allowed_channel_types", postgresql.ARRAY(sa.String(32)), nullable=True),
        )
    op.execute(PLAN_VERSION_ENTITLEMENT_TERMS_FUNCTION)
    op.execute(PLAN_VERSION_ENTITLEMENT_TERMS_TRIGGER)

    # 3. Typed top-ups and plan eligibility.
    for table in ("topup_products", "topup_purchases"):
        op.add_column(table, sa.Column("channel_type", channel, nullable=True))
    # A WhatsApp-number product sold WhatsApp slots: it becomes one.
    op.execute(
        "UPDATE topup_products SET entitlement_key = 'channel_connections',"
        " channel_type = 'whatsapp' WHERE entitlement_key::text = 'whatsapp_numbers'"
    )
    for table in ("topup_products", "topup_purchases"):
        name = TYPED_CONSTRAINT.format(table=table)
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({TYPED_CHECK}) NOT VALID")
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
    op.execute(TOPUP_SNAPSHOT_FUNCTION)
    op.execute(TOPUP_RETIRED_KEY_FUNCTION)
    op.execute(TOPUP_PRODUCTS_RETIRED_KEY_TRIGGER)
    op.execute(TOPUP_PURCHASES_RETIRED_KEY_TRIGGER)
    op.create_table(
        "topup_product_plans",
        sa.Column("topup_product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["topup_product_id"],
            ["topup_products.id"],
            name="fk_topup_product_plans_topup_product_id_topup_products",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name="fk_topup_product_plans_plan_id_plans",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("topup_product_id", "plan_id", name="pk_topup_product_plans"),
    )
    op.create_index("ix_topup_product_plans_plan_id", "topup_product_plans", ["plan_id"])

    # 4. Why a connection was disabled.
    op.add_column(
        "channel_connections",
        sa.Column(
            "disabled_reason",
            postgresql.ENUM(name="connection_disabled_reason", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "channel_connections",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_connections",
        sa.Column("disabled_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_channel_connections_disabled_by_users",
        "channel_connections",
        "users",
        ["disabled_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse()
    op.execute(
        "ALTER TABLE channel_connections DROP CONSTRAINT fk_channel_connections_disabled_by_users"
    )
    for column in ("disabled_by", "disabled_at", "disabled_reason"):
        op.drop_column("channel_connections", column)
    op.execute("DROP TYPE IF EXISTS connection_disabled_reason")

    op.drop_index("ix_topup_product_plans_plan_id", table_name="topup_product_plans")
    op.drop_table("topup_product_plans")
    op.execute("DROP TRIGGER IF EXISTS topup_purchases_retired_key ON topup_purchases")
    op.execute("DROP TRIGGER IF EXISTS topup_products_retired_key ON topup_products")
    op.execute("DROP FUNCTION IF EXISTS topup_refuse_retired_key()")
    op.execute(TOPUP_SNAPSHOT_FUNCTION_0072)
    # The converted WhatsApp products go back to the key they were sold under.
    op.execute(
        "UPDATE topup_products SET entitlement_key = 'whatsapp_numbers', channel_type = NULL"
        " WHERE entitlement_key::text = 'channel_connections' AND channel_type = 'whatsapp'"
    )
    for table in ("topup_purchases", "topup_products"):
        # Named in full: `op.drop_constraint` would apply the naming convention
        # to an already-conventional name.
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {TYPED_CONSTRAINT.format(table=table)}")
        op.drop_column(table, "channel_type")

    op.execute("DROP TRIGGER IF EXISTS plan_versions_entitlement_terms ON plan_versions")
    op.execute("DROP FUNCTION IF EXISTS plan_versions_entitlement_terms()")
    for table in ("plan_versions", "plans"):
        op.drop_column(table, "allowed_channel_types")
    op.execute("RESET lock_timeout")
