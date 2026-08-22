"""create the campaign tables and contact opt-out

Revision ID: 0013
Revises: 0012

`campaign_recipients` carries one row per person a campaign is owed to, and
`UNIQUE(campaign_id, contact_id)` is what makes a broadcast safe to restart: a
worker killed halfway through ten thousand sends must not send the first
thousand again, and that is a constraint's job rather than a service's.

Both partial indexes are the working queries. `ix_campaigns_due` is the worker's
only campaign lookup, and `ix_campaign_recipients_pending` is the one that runs
once per batch for the life of a campaign; making either of them cover finished
rows would make every sweep slower as history accumulates.

`contacts` gains an opt-out. A timestamp rather than a boolean, because "since
when" is the question a dispute about a marketing message actually turns on, and
nullable rather than defaulted because nobody has opted out until they say so.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

CAMPAIGN_STATUS = postgresql.ENUM(
    "draft",
    "scheduled",
    "running",
    "paused",
    "completed",
    "cancelled",
    "failed",
    name="campaign_status",
    create_type=False,
)
RECIPIENT_STATUS = postgresql.ENUM(
    "pending",
    "sent",
    "failed",
    "skipped",
    name="recipient_status",
    create_type=False,
)
OPT_OUT_SOURCE = postgresql.ENUM(
    "customer",
    "team",
    name="opt_out_source",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    CAMPAIGN_STATUS.create(bind, checkfirst=False)
    RECIPIENT_STATUS.create(bind, checkfirst=False)
    OPT_OUT_SOURCE.create(bind, checkfirst=False)

    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", CAMPAIGN_STATUS, nullable=False),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audience", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audience_size", sa.Integer(), nullable=False),
        sa.Column("messages_per_minute", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
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
            name="fk_campaigns_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
            name="fk_campaigns_account_id_whatsapp_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["whatsapp_templates.id"],
            name="fk_campaigns_template_id_whatsapp_templates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_campaigns_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaigns"),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])
    op.create_index("ix_campaigns_tenant_id_status", "campaigns", ["tenant_id", "status"])
    op.create_index("ix_campaigns_tenant_id_created_at", "campaigns", ["tenant_id", "created_at"])
    op.create_index("ix_campaigns_account_id", "campaigns", ["account_id"])
    op.create_index("ix_campaigns_template_id", "campaigns", ["template_id"])
    op.create_index(
        "ix_campaigns_due",
        "campaigns",
        ["scheduled_at"],
        postgresql_where=sa.text("status IN ('scheduled', 'running')"),
    )

    op.create_table(
        "campaign_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", RECIPIENT_STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_campaign_recipients_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_campaign_recipients_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_campaign_recipients_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_campaign_recipients_conversation_id_conversations",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_campaign_recipients_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_recipients"),
        sa.UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_campaign_recipients_campaign_id_contact_id",
        ),
    )
    op.create_index("ix_campaign_recipients_tenant_id", "campaign_recipients", ["tenant_id"])
    op.create_index(
        "ix_campaign_recipients_campaign_id_status",
        "campaign_recipients",
        ["campaign_id", "status"],
    )
    op.create_index("ix_campaign_recipients_contact_id", "campaign_recipients", ["contact_id"])
    op.create_index(
        "ix_campaign_recipients_pending",
        "campaign_recipients",
        ["campaign_id", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.add_column(
        "contacts",
        sa.Column("marketing_opt_out_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("contacts", sa.Column("opt_out_source", OPT_OUT_SOURCE, nullable=True))


def downgrade():
    bind = op.get_bind()

    op.drop_column("contacts", "opt_out_source")
    op.drop_column("contacts", "marketing_opt_out_at")

    op.drop_index("ix_campaign_recipients_pending", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_contact_id", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_campaign_id_status", table_name="campaign_recipients")
    op.drop_index("ix_campaign_recipients_tenant_id", table_name="campaign_recipients")
    op.drop_table("campaign_recipients")

    op.drop_index("ix_campaigns_due", table_name="campaigns")
    op.drop_index("ix_campaigns_template_id", table_name="campaigns")
    op.drop_index("ix_campaigns_account_id", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id_created_at", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id_status", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")

    OPT_OUT_SOURCE.drop(bind, checkfirst=False)
    RECIPIENT_STATUS.drop(bind, checkfirst=False)
    CAMPAIGN_STATUS.drop(bind, checkfirst=False)
