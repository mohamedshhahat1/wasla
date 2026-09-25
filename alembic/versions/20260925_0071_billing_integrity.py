"""Versioned plans, advance billing and a protected financial ledger.

Revision ID: 0071
Revises: 0070

The schema half of the billing remediation (audit billing-03bb13b1).

**Plan versions (BILL-12).** `plan_versions` holds immutable commercial terms -
price, currency, interval, limits - and a trigger refuses any UPDATE. Every
existing plan gets version 1, snapshotted from its row, effective from the
epoch: it is the terms the plan has always had, not a new offer. Every existing
subscription is pinned to its plan's version 1, which *is* the catalogue at
migration time. Historical prices before this migration cannot be reconstructed
beyond what invoices recorded (`plan_code`, `amount_due`, `lines`), and no older
version is invented.

**Subscriptions** gain the pinned version, a billing anchor (BILL-18) - set to
the current period start, since the original day of a subscription whose day was
already clamped by a short month cannot be recovered - a scheduled plan change,
and a revision counter for optimistic concurrency.

**Invoices** gain a purpose (BILL-02): one already issued by the sweep
(`issued_at` set) is a `renewal`, anything else a `checkout`. Only a renewal is
ever collected automatically. The period uniqueness becomes renewal-only, since
two checkouts are two purchases (BILL-06). An *open* renewal for its
subscription's current period, whose amount equals version 1's price, is pinned
to version 1 so it stays collectible; no other historical invoice is given a
version.

**Payments** gain the Paymob order id (BILL-04/-11), mode, settling integration
and the requested refund total. No order id is invented for an existing
payment. A pending checkout created before this migration cannot be bound to a
callback: a collection callback for it is refused and raised as an incident for
an operator, the fail-safe answer (see docs/BILLING.md). An outstanding refund
request keeps its old full-remainder meaning.

**Money constraints (BILL-12/-20)** are added `NOT VALID` - enforced on every new
write - and then validated if existing data allows. Data that already violates
one (an overpaid invoice from the old manual path, say) leaves that constraint
unvalidated rather than rewriting financial history, and says so in the log.

**Retention (BILL-19).** `invoices`, `payments` and `payment_events` no longer
cascade from their tenant or parent: deleting a tenant row that still has a
ledger is refused.

New tables: `plan_version_migrations`, `billing_adjustments`,
`billing_incidents`. New audit labels are added last, outside the transaction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

INVOICE_PURPOSES = ("checkout", "renewal", "manual", "adjustment")
CHANGE_SOURCES = ("downgrade", "operator", "migration")
ADJUSTMENT_KINDS = ("complimentary_grant", "invoice_waiver")
INCIDENT_KINDS = (
    "duplicate_payment",
    "refused_settlement",
    "mismatched_callback",
    "unknown_callback",
    "permanent_provider_error",
    "recovered_by_reconciliation",
    "refund_requested",
)
INCIDENT_STATUSES = ("open", "resolved")

NEW_ACTIONS = (
    "subscription_reactivated",
    "subscription_plan_change_scheduled",
    "subscription_scheduled_change_cancelled",
    "subscription_complimentary_grant",
    "payment_refund_review_requested",
    "billing_plan_created",
    "billing_plan_updated",
    "billing_plan_version_created",
    "billing_plan_activated",
    "billing_plan_deactivated",
    "billing_plan_deleted",
    "billing_plan_migration_scheduled",
    "billing_reconciliation_started",
    "billing_reconciliation_resolved",
    "billing_incident_resolved",
    "platform_billing_read",
)

# (table, constraint name, expression) - restated rather than imported, so the
# migration keeps meaning what it meant when the models move on.
CHECKS = (
    ("plans", "ck_plans_price_non_negative", "price >= 0"),
    ("plans", "ck_plans_trial_days_non_negative", "trial_days >= 0"),
    ("plans", "ck_plans_currency_supported", "currency = 'EGP'"),
    ("invoices", "ck_invoices_amount_due_non_negative", "amount_due >= 0"),
    ("invoices", "ck_invoices_amount_paid_non_negative", "amount_paid >= 0"),
    ("invoices", "ck_invoices_amount_paid_within_due", "amount_paid <= amount_due"),
    ("invoices", "ck_invoices_currency_supported", "currency = 'EGP'"),
    ("payments", "ck_payments_amount_positive", "amount > 0"),
    ("payments", "ck_payments_refunded_amount_non_negative", "refunded_amount >= 0"),
    ("payments", "ck_payments_refunded_within_amount", "refunded_amount <= amount"),
    ("payments", "ck_payments_currency_supported", "currency = 'EGP'"),
)

# (table, constraint, column, referred table, old ondelete, new ondelete)
FOREIGN_KEYS = (
    ("invoices", "fk_invoices_tenant_id_tenants", "tenant_id", "tenants", "CASCADE", "RESTRICT"),
    ("payments", "fk_payments_tenant_id_tenants", "tenant_id", "tenants", "CASCADE", "RESTRICT"),
    (
        "payments",
        "fk_payments_invoice_id_invoices",
        "invoice_id",
        "invoices",
        "CASCADE",
        "RESTRICT",
    ),
    (
        "payment_events",
        "fk_payment_events_payment_id_payments",
        "payment_id",
        "payments",
        "CASCADE",
        "RESTRICT",
    ),
)

IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION plan_versions_refuse_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'plan_versions rows are immutable; publish a new version'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""
IMMUTABLE_TRIGGER = (
    "CREATE TRIGGER plan_versions_immutable BEFORE UPDATE ON plan_versions "
    "FOR EACH ROW EXECUTE FUNCTION plan_versions_refuse_update()"
)


def _uuid_fk(table: str, column: str, target: str, *, ondelete: str) -> sa.Column:
    """A nullable UUID foreign key, named by the models' naming convention."""
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(f"{target}.id", ondelete=ondelete, name=f"fk_{table}_{column}_{target}"),
        nullable=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    purpose = postgresql.ENUM(*INVOICE_PURPOSES, name="invoice_purpose", create_type=False)
    source = postgresql.ENUM(*CHANGE_SOURCES, name="scheduled_change_source", create_type=False)
    adjustment = postgresql.ENUM(
        *ADJUSTMENT_KINDS, name="billing_adjustment_kind", create_type=False
    )
    incident_kind = postgresql.ENUM(
        *INCIDENT_KINDS, name="billing_incident_kind", create_type=False
    )
    incident_status = postgresql.ENUM(
        *INCIDENT_STATUSES, name="billing_incident_status", create_type=False
    )
    for enum in (purpose, source, adjustment, incident_kind, incident_status):
        enum.create(bind, checkfirst=True)
    interval = postgresql.ENUM(name="billing_interval", create_type=False)

    # ---------------------------------------------------------- plan versions
    op.create_table(
        "plan_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="CASCADE", name="fk_plan_versions_plan_id_plans"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("interval", interval, nullable=False),
        sa.Column("trial_days", sa.Integer(), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_plan_versions_created_by_users"
            ),
            nullable=True,
        ),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_plan_versions"),
        sa.UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_id_version"),
        sa.CheckConstraint("version >= 1", name=op.f("ck_plan_versions_version_positive")),
        sa.CheckConstraint("price >= 0", name=op.f("ck_plan_versions_price_non_negative")),
        sa.CheckConstraint(
            "trial_days >= 0", name=op.f("ck_plan_versions_trial_days_non_negative")
        ),
        sa.CheckConstraint("currency = 'EGP'", name=op.f("ck_plan_versions_currency_supported")),
    )
    op.create_index(
        "ix_plan_versions_plan_id_effective_at", "plan_versions", ["plan_id", "effective_at"]
    )
    op.execute("""
        INSERT INTO plan_versions
            (id, plan_id, version, name, price, currency, interval, trial_days, limits,
             effective_at, created_at, created_by, reason)
        SELECT gen_random_uuid(), p.id, 1, p.name, p.price, p.currency, p.interval,
               p.trial_days, p.limits, TIMESTAMPTZ '1970-01-01 00:00:00+00', now(), NULL,
               'Initial version, snapshotted from the plan catalogue by migration 0071.'
        FROM plans p
        """)
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(IMMUTABLE_TRIGGER)
    op.add_column("plans", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))

    # ---------------------------------------------------------- subscriptions
    op.add_column(
        "subscriptions",
        _uuid_fk("subscriptions", "plan_version_id", "plan_versions", ondelete="RESTRICT"),
    )
    op.add_column(
        "subscriptions", sa.Column("billing_anchor_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscriptions",
        _uuid_fk(
            "subscriptions", "scheduled_plan_version_id", "plan_versions", ondelete="RESTRICT"
        ),
    )
    op.add_column("subscriptions", sa.Column("scheduled_change_source", source, nullable=True))
    op.add_column(
        "subscriptions", sa.Column("scheduled_change_reason", sa.String(500), nullable=True)
    )
    op.add_column(
        "subscriptions",
        _uuid_fk("subscriptions", "scheduled_change_actor_id", "users", ondelete="SET NULL"),
    )
    op.add_column(
        "subscriptions", sa.Column("scheduled_change_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscriptions",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute("""
        UPDATE subscriptions s
        SET plan_version_id = v.id,
            -- The period start when the period is exactly one interval from it
            -- (every period the application opened), else the period end - the
            -- same rule `subscription_service.legacy_anchor` applies.
            billing_anchor_at = CASE
                WHEN s.current_period_start + CASE v.interval
                        WHEN 'yearly' THEN interval '1 year'
                        ELSE interval '1 month'
                    END = s.current_period_end
                THEN s.current_period_start
                ELSE s.current_period_end
            END
        FROM plan_versions v
        WHERE v.plan_id = s.plan_id AND v.version = 1
        """)

    # --------------------------------------------------------------- invoices
    op.add_column("invoices", sa.Column("purpose", purpose, nullable=True))
    op.execute("""
        UPDATE invoices
        SET purpose = CASE WHEN issued_at IS NOT NULL THEN 'renewal' ELSE 'checkout' END
            ::invoice_purpose
        """)
    op.alter_column("invoices", "purpose", nullable=False)
    op.add_column(
        "invoices", _uuid_fk("invoices", "plan_version_id", "plan_versions", ondelete="RESTRICT")
    )
    op.add_column(
        "invoices", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.execute("""
        UPDATE invoices i
        SET plan_version_id = s.plan_version_id
        FROM subscriptions s
        JOIN plan_versions v ON v.id = s.plan_version_id
        WHERE i.subscription_id = s.id
          AND i.purpose = 'renewal'
          AND i.status = 'open'
          AND i.period_start = s.current_period_start
          AND i.amount_due = v.price
        """)
    op.drop_constraint("uq_invoices_tenant_id_period_start", "invoices", type_="unique")
    op.create_index(
        "uq_invoices_renewal_tenant_id_period_start",
        "invoices",
        ["tenant_id", "period_start"],
        unique=True,
        postgresql_where=sa.text("purpose = 'renewal'"),
    )
    op.create_index("ix_invoices_purpose_status", "invoices", ["purpose", "status"])

    # --------------------------------------------------------------- payments
    op.add_column("payments", sa.Column("provider_order_id", sa.String(200), nullable=True))
    op.add_column("payments", sa.Column("provider_mode", sa.String(10), nullable=True))
    op.add_column("payments", sa.Column("provider_integration_id", sa.String(50), nullable=True))
    op.add_column(
        "payments", sa.Column("refund_requested_amount", sa.Numeric(12, 2), nullable=True)
    )
    op.add_column(
        "payments", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.execute("""
        UPDATE payments SET refund_requested_amount = amount
        WHERE refund_requested_at IS NOT NULL AND refunded_at IS NULL
        """)
    op.create_index(
        "ix_payments_provider_provider_order_id", "payments", ["provider", "provider_order_id"]
    )

    # ------------------------------------------------------------ new tables
    op.create_table(
        "plan_version_migrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plans.id", ondelete="RESTRICT", name="fk_plan_version_migrations_plan_id_plans"
            ),
            nullable=False,
        ),
        sa.Column(
            "from_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plan_versions.id",
                ondelete="RESTRICT",
                name="fk_plan_version_migrations_from_version_id_plan_versions",
            ),
            nullable=False,
        ),
        sa.Column(
            "to_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plan_versions.id",
                ondelete="RESTRICT",
                name="fk_plan_version_migrations_to_version_id_plan_versions",
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_plan_version_migrations_created_by_users"
            ),
            nullable=True,
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan_version_migrations"),
        sa.CheckConstraint(
            "from_version_id <> to_version_id",
            name=op.f("ck_plan_version_migrations_distinct_versions"),
        ),
    )
    op.create_index(
        "ix_plan_version_migrations_from_version_id",
        "plan_version_migrations",
        ["from_version_id"],
    )
    op.create_index(
        "uq_plan_version_migrations_live_from",
        "plan_version_migrations",
        ["from_version_id"],
        unique=True,
        postgresql_where=sa.text("cancelled_at IS NULL"),
    )

    op.create_table(
        "billing_adjustments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="RESTRICT", name="fk_billing_adjustments_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "subscriptions.id",
                ondelete="SET NULL",
                name="fk_billing_adjustments_subscription_id_subscriptions",
            ),
            nullable=True,
        ),
        sa.Column("kind", adjustment, nullable=False),
        sa.Column(
            "plan_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plan_versions.id",
                ondelete="RESTRICT",
                name="fk_billing_adjustments_plan_version_id_plan_versions",
            ),
            nullable=True,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "invoices.id",
                ondelete="RESTRICT",
                name="fk_billing_adjustments_invoice_id_invoices",
            ),
            nullable=True,
        ),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_billing_adjustments_actor_id_users"
            ),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_adjustments"),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name=op.f("ck_billing_adjustments_window_ordered"),
        ),
    )
    op.create_index("ix_billing_adjustments_tenant_id", "billing_adjustments", ["tenant_id"])
    op.create_index(
        "ix_billing_adjustments_subscription_id", "billing_adjustments", ["subscription_id"]
    )

    op.create_table(
        "billing_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="RESTRICT", name="fk_billing_incidents_tenant_id_tenants"
            ),
            nullable=True,
        ),
        sa.Column("kind", incident_kind, nullable=False),
        sa.Column("status", incident_status, nullable=False),
        sa.Column("dedupe_key", sa.String(250), nullable=False),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "payments.id", ondelete="RESTRICT", name="fk_billing_incidents_payment_id_payments"
            ),
            nullable=True,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "invoices.id", ondelete="RESTRICT", name="fk_billing_incidents_invoice_id_invoices"
            ),
            nullable=True,
        ),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("provider_transaction_id", sa.String(200), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_billing_incidents_resolved_by_users"
            ),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_incidents"),
        sa.UniqueConstraint("dedupe_key", name="uq_billing_incidents_dedupe_key"),
    )
    op.create_index("ix_billing_incidents_tenant_id", "billing_incidents", ["tenant_id"])
    op.create_index("ix_billing_incidents_status_kind", "billing_incidents", ["status", "kind"])
    op.create_index("ix_billing_incidents_payment_id", "billing_incidents", ["payment_id"])

    # ------------------------------------------------------ foreign key policy
    for table, name, column, target, _old, new in FOREIGN_KEYS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, target, [column], ["id"], ondelete=new)

    # ------------------------------------------------------ money constraints
    for table, name, expression in CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
        # Validated in a savepoint of its own: existing data that breaks a rule
        # leaves that one constraint enforced-for-new-writes-only rather than
        # failing the deployment or rewriting history.
        op.execute(f"""
            DO $$
            BEGIN
                ALTER TABLE {table} VALIDATE CONSTRAINT {name};
            EXCEPTION WHEN check_violation THEN
                RAISE WARNING 'existing rows violate {name}; left NOT VALID';
            END $$;
            """)

    # Last, and outside the transaction: `ADD VALUE` cannot run inside one.
    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Return to the 0070 schema.

    Versions, adjustments and incidents are dropped with their tables; the
    ledger's cascade semantics and the all-invoices period uniqueness return.
    The audit labels stay: PostgreSQL cannot drop an enum label.
    """
    for table, name, _expression in reversed(CHECKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    for table, name, column, target, old, _new in FOREIGN_KEYS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, target, [column], ["id"], ondelete=old)

    op.drop_table("billing_incidents")
    op.drop_table("billing_adjustments")
    op.drop_table("plan_version_migrations")

    op.drop_index("ix_payments_provider_provider_order_id", table_name="payments")
    for column in (
        "revision",
        "refund_requested_amount",
        "provider_integration_id",
        "provider_mode",
        "provider_order_id",
    ):
        op.drop_column("payments", column)

    op.drop_index("ix_invoices_purpose_status", table_name="invoices")
    op.drop_index("uq_invoices_renewal_tenant_id_period_start", table_name="invoices")
    op.create_unique_constraint(
        "uq_invoices_tenant_id_period_start", "invoices", ["tenant_id", "period_start"]
    )
    for column in ("revision", "plan_version_id", "purpose"):
        op.drop_column("invoices", column)

    for column in (
        "revision",
        "scheduled_change_at",
        "scheduled_change_actor_id",
        "scheduled_change_reason",
        "scheduled_change_source",
        "scheduled_plan_version_id",
        "billing_anchor_at",
        "plan_version_id",
    ):
        op.drop_column("subscriptions", column)
    op.drop_column("plans", "revision")

    op.execute("DROP TRIGGER IF EXISTS plan_versions_immutable ON plan_versions")
    op.execute("DROP FUNCTION IF EXISTS plan_versions_refuse_update()")
    op.drop_table("plan_versions")

    bind = op.get_bind()
    for name in (
        "billing_incident_status",
        "billing_incident_kind",
        "billing_adjustment_kind",
        "scheduled_change_source",
        "invoice_purpose",
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
