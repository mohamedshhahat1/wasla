"""create the invoice and payment tables

Revision ID: 0017
Revises: 0016

Two constraints carry this migration, and both exist because the alternative is
taking money twice.

`UNIQUE(tenant_id, period_start)` on invoices: a sweep that runs twice, or two
replicas sweeping at once, must not bill a workspace twice for March. A check in
a service cannot promise that across replicas; a constraint can.

`UNIQUE(provider, provider_reference)` on payments: a processor's idempotency
key. Two webhooks describing the same charge become one payment row, and a
retried request collects once.

The foreign keys differ deliberately. An invoice's subscription is `SET NULL`,
because an invoice outlives the arrangement it came from - a customer who left
last year can still be shown what they paid. A payment's invoice is `CASCADE`,
because a payment without its invoice is not a record of anything.

Amounts are `Numeric(12, 2)`, never float: 19.99 has no exact binary floating
point representation, and this is the table where that error would be printed
and sent to somebody.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

INVOICE_STATUS = postgresql.ENUM(
    "draft",
    "open",
    "paid",
    "uncollectible",
    "void",
    name="invoice_status",
    create_type=False,
)
PAYMENT_STATUS = postgresql.ENUM(
    "pending",
    "succeeded",
    "failed",
    "refunded",
    name="payment_status",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    INVOICE_STATUS.create(bind, checkfirst=False)
    PAYMENT_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", INVOICE_STATUS, nullable=False),
        sa.Column("plan_code", sa.String(length=50), nullable=False),
        sa.Column("amount_due", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_invoices_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name="fk_invoices_subscription_id_subscriptions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint(
            "tenant_id",
            "period_start",
            name="uq_invoices_tenant_id_period_start",
        ),
    )
    op.create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])
    op.create_index("ix_invoices_tenant_id_status", "invoices", ["tenant_id", "status"])
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_subscription_id", "invoices", ["subscription_id"])

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", PAYMENT_STATUS, nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_reference", sa.String(length=200), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_payments_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_payments_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint(
            "provider",
            "provider_reference",
            name="uq_payments_provider_provider_reference",
        ),
    )
    op.create_index("ix_payments_tenant_id", "payments", ["tenant_id"])
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"])
    op.create_index("ix_payments_status", "payments", ["status"])


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_invoice_id", table_name="payments")
    op.drop_index("ix_payments_tenant_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_invoices_subscription_id", table_name="invoices")
    op.drop_index("ix_invoices_status", table_name="invoices")
    op.drop_index("ix_invoices_tenant_id_status", table_name="invoices")
    op.drop_index("ix_invoices_tenant_id", table_name="invoices")
    op.drop_table("invoices")

    PAYMENT_STATUS.drop(bind, checkfirst=False)
    INVOICE_STATUS.drop(bind, checkfirst=False)
