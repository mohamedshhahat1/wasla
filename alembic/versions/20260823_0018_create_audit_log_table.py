"""create the audit log table

Revision ID: 0018
Revises: 0017

One append-only table, and its foreign keys are the design.

`tenant_id` is **nullable** and `SET NULL` on delete: a platform administrator
acts across workspaces rather than inside one, and those acts are the ones most
worth recording. A workspace being deleted must not take the record of who
deleted it with it.

`actor_id` is `SET NULL` for the same reason, never `CASCADE`. Deleting an
account must not erase what that account did — which is exactly what somebody
would do if it worked. `actor_label` carries a copy of the email so the entry
stays readable afterwards.

The target is an opaque `(type, id, label)` rather than a foreign key. It may be
a row in any of a dozen tables, and half the interesting entries describe
something that has since been deleted; a real key would either forbid that or
blank the entry when it happened.

Indexes are the three reads: a workspace's own trail, the platform's whole
trail, and everything of one kind. All are `(…, occurred_at)` because a log is
always read newest first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

AUDIT_ACTOR_KIND = postgresql.ENUM(
    "user",
    "platform_staff",
    "system",
    name="audit_actor_kind",
    create_type=False,
)
AUDIT_ACTION = postgresql.ENUM(
    "member_invited",
    "invitation_revoked",
    "invitation_accepted",
    "whatsapp_account_connected",
    "whatsapp_account_disabled",
    "whatsapp_account_enabled",
    "subscription_started",
    "subscription_plan_changed",
    "subscription_cancelled",
    "subscription_resumed",
    "payment_recorded",
    "invoice_voided",
    "campaign_scheduled",
    "campaign_cancelled",
    name="audit_action",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    AUDIT_ACTOR_KIND.create(bind, checkfirst=False)
    AUDIT_ACTION.create(bind, checkfirst=False)

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", AUDIT_ACTION, nullable=False),
        sa.Column("actor_kind", AUDIT_ACTOR_KIND, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(length=320), nullable=True),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_label", sa.String(length=200), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_audit_logs_tenant_id_tenants",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_audit_logs_actor_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index(
        "ix_audit_logs_tenant_id_occurred_at",
        "audit_logs",
        ["tenant_id", "occurred_at"],
    )
    op.create_index("ix_audit_logs_occurred_at", "audit_logs", ["occurred_at"])
    op.create_index(
        "ix_audit_logs_action_occurred_at",
        "audit_logs",
        ["action", "occurred_at"],
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_occurred_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_occurred_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_id_occurred_at", table_name="audit_logs")
    op.drop_table("audit_logs")

    AUDIT_ACTION.drop(bind, checkfirst=False)
    AUDIT_ACTOR_KIND.drop(bind, checkfirst=False)
