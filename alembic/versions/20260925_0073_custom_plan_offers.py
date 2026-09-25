"""Custom plan offers: a priced custom plan is accepted and paid, never just assigned.

Revision ID: 0073
Revises: 0072

**`custom_plan_offers` (ADR-114).** One row per offer of one immutable version
of a workspace's own custom plan, moving `offered -> pending_payment -> active`
or ending `declined`, `expired` or `cancelled`. A partial unique index keeps at
most one open offer per workspace. A trigger fixes an offer's tenant, plan and
version for ever and refuses any offer of a plan that is not the offering
workspace's own custom plan.

**`invoices.custom_plan_offer_id`.** The checkout an owner opens by accepting
an offer names it, through a composite foreign key onto `(id, tenant_id)` so an
invoice can only ever name its own workspace's offer. A CHECK allows it only on
a `checkout` invoice with a pinned version, and a trigger requires that version
to be the offered one and refuses re-pointing the invoice at another offer.

Enum labels (six audit actions) are added last, outside the transaction. The
downgrade leaves them, as PostgreSQL cannot drop a label.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

OFFER_STATUSES = ("offered", "pending_payment", "active", "declined", "expired", "cancelled")
NEW_ACTIONS = (
    "billing_custom_plan_offered",
    "billing_custom_plan_offer_accepted",
    "billing_custom_plan_offer_declined",
    "billing_custom_plan_offer_activated",
    "billing_custom_plan_offer_cancelled",
    "billing_custom_plan_offer_expired",
)
MAX_REASON = 500

# Restated rather than imported, so this migration keeps meaning what it meant
# when the models move on.
OFFER_INTEGRITY_FUNCTION = """
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
OFFER_INTEGRITY_TRIGGER = (
    "CREATE TRIGGER custom_plan_offers_integrity BEFORE INSERT OR UPDATE ON custom_plan_offers "
    "FOR EACH ROW EXECUTE FUNCTION custom_plan_offers_refuse_foreign_or_changed()"
)
INVOICE_OFFER_FUNCTION = """
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
INVOICE_OFFER_TRIGGER = (
    "CREATE TRIGGER invoices_custom_plan_offer BEFORE INSERT OR UPDATE OF "
    "custom_plan_offer_id, plan_version_id ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_offer_mismatch()"
)


def _user(column: str) -> sa.Column:
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            "users.id", ondelete="SET NULL", name=f"fk_custom_plan_offers_{column}_users"
        ),
        nullable=True,
    )


def upgrade() -> None:
    status = postgresql.ENUM(*OFFER_STATUSES, name="custom_plan_offer_status", create_type=False)
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "custom_plan_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="RESTRICT", name="fk_custom_plan_offers_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plans.id", ondelete="RESTRICT", name="fk_custom_plan_offers_plan_id_plans"
            ),
            nullable=False,
        ),
        sa.Column(
            "plan_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "plan_versions.id",
                ondelete="RESTRICT",
                name="fk_custom_plan_offers_plan_version_id_plan_versions",
            ),
            nullable=False,
        ),
        sa.Column("status", status, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(MAX_REASON), nullable=False),
        _user("created_by"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        _user("accepted_by"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        _user("declined_by"),
        sa.Column("decline_reason", sa.String(MAX_REASON), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        _user("cancelled_by"),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_custom_plan_offers_id_tenant_id"),
        sa.CheckConstraint(
            "status NOT IN ('pending_payment', 'active') OR accepted_at IS NOT NULL",
            name=op.f("ck_custom_plan_offers_accepted_has_moment"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR activated_at IS NOT NULL",
            name=op.f("ck_custom_plan_offers_active_has_moment"),
        ),
        sa.CheckConstraint(
            "status <> 'declined' OR declined_at IS NOT NULL",
            name=op.f("ck_custom_plan_offers_declined_has_moment"),
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name=op.f("ck_custom_plan_offers_cancelled_has_moment"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_custom_plan_offers"),
    )
    op.create_index(
        "uq_custom_plan_offers_one_open_per_tenant",
        "custom_plan_offers",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('offered', 'pending_payment')"),
    )
    op.create_index(
        "ix_custom_plan_offers_tenant_id_created_at",
        "custom_plan_offers",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_custom_plan_offers_plan_version_id", "custom_plan_offers", ["plan_version_id"]
    )
    op.create_index(
        "ix_custom_plan_offers_status_expires_at", "custom_plan_offers", ["status", "expires_at"]
    )

    op.add_column(
        "invoices",
        sa.Column("custom_plan_offer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_invoices_custom_plan_offer_tenant",
        "invoices",
        "custom_plan_offers",
        ["custom_plan_offer_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_invoices_offer_is_a_checkout"),
        "invoices",
        "custom_plan_offer_id IS NULL OR (purpose = 'checkout' AND plan_version_id IS NOT NULL)",
    )
    op.create_index(
        "ix_invoices_custom_plan_offer_id",
        "invoices",
        ["custom_plan_offer_id"],
        postgresql_where=sa.text("custom_plan_offer_id IS NOT NULL"),
    )

    for statement in (
        OFFER_INTEGRITY_FUNCTION,
        OFFER_INTEGRITY_TRIGGER,
        INVOICE_OFFER_FUNCTION,
        INVOICE_OFFER_TRIGGER,
    ):
        op.execute(statement)

    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Refused while any invoice names an offer: dropping the column would
    # erase which checkout bought which negotiated terms.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM invoices WHERE custom_plan_offer_id IS NOT NULL) THEN
                RAISE EXCEPTION 'invoices name custom plan offers; refusing to downgrade';
            END IF;
        END
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS invoices_custom_plan_offer ON invoices")
    op.execute("DROP FUNCTION IF EXISTS invoices_refuse_offer_mismatch()")
    op.drop_index("ix_invoices_custom_plan_offer_id", table_name="invoices")
    op.drop_constraint(op.f("ck_invoices_offer_is_a_checkout"), "invoices", type_="check")
    op.drop_constraint("fk_invoices_custom_plan_offer_tenant", "invoices", type_="foreignkey")
    op.drop_column("invoices", "custom_plan_offer_id")
    op.execute("DROP TRIGGER IF EXISTS custom_plan_offers_integrity ON custom_plan_offers")
    op.execute("DROP FUNCTION IF EXISTS custom_plan_offers_refuse_foreign_or_changed()")
    op.drop_table("custom_plan_offers")
    op.execute("DROP TYPE IF EXISTS custom_plan_offer_status")
