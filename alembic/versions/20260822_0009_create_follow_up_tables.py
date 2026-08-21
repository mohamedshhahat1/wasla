"""create the follow-up table

Revision ID: 0009
Revises: 0008

Two partial indexes carry real weight here and neither is an optimisation.

``uq_follow_ups_pending_conversation`` is what makes "one waiting nudge per
conversation" a database guarantee: an agent that decides to schedule on every
turn would otherwise queue five messages at one customer. Partial, so a
conversation already followed up once can be followed up again later.

``ix_follow_ups_due`` covers the only query the worker runs — pending rows whose
time has come. Partial for a different reason: everything else in this table is
finished work, and history accumulates forever, so a full index would make the
sweep slower every week it runs.

``ondelete`` differs by relationship. The conversation owns the follow-up and
cascades. The lead and the sent message do not: the nudge is owed to the
customer whether or not the opportunity survives, and deleting a message must
not erase the evidence that one went out.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

FOLLOW_UP_STATUS = postgresql.ENUM(
    "pending",
    "sent",
    "cancelled",
    "failed",
    "skipped",
    name="follow_up_status",
    create_type=False,
)
# actor_kind already exists from migration 0008. Referenced without creating it,
# because two migrations creating one type is an error on the second.
ACTOR_KIND = postgresql.ENUM(
    "user",
    "agent",
    "system",
    name="actor_kind",
    create_type=False,
)

# Both match the expressions declared on the model. Any difference and
# ``alembic check`` reports drift on every run.
PENDING_PREDICATE = "status = 'pending'"


def upgrade():
    bind = op.get_bind()
    FOLLOW_UP_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "follow_ups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", FOLLOW_UP_STATUS, nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("template_name", sa.String(length=512), nullable=True),
        sa.Column("template_language", sa.String(length=16), nullable=True),
        sa.Column("template_components", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_kind", ACTOR_KIND, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.String(length=300), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_follow_ups"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_follow_ups_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_follow_ups_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_follow_ups_lead_id_leads",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_follow_ups_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_follow_ups_message_id_messages",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_follow_ups_tenant_id", "follow_ups", ["tenant_id"])
    op.create_index("ix_follow_ups_tenant_id_status", "follow_ups", ["tenant_id", "status"])
    op.create_index("ix_follow_ups_conversation_id", "follow_ups", ["conversation_id"])
    op.create_index("ix_follow_ups_lead_id", "follow_ups", ["lead_id"])
    op.create_index(
        "ix_follow_ups_tenant_id_created_at",
        "follow_ups",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_follow_ups_due",
        "follow_ups",
        ["scheduled_at"],
        postgresql_where=sa.text(PENDING_PREDICATE),
    )
    op.create_index(
        "uq_follow_ups_pending_conversation",
        "follow_ups",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(PENDING_PREDICATE),
    )


def downgrade():
    op.drop_index("uq_follow_ups_pending_conversation", table_name="follow_ups")
    op.drop_index("ix_follow_ups_due", table_name="follow_ups")
    op.drop_index("ix_follow_ups_tenant_id_created_at", table_name="follow_ups")
    op.drop_index("ix_follow_ups_lead_id", table_name="follow_ups")
    op.drop_index("ix_follow_ups_conversation_id", table_name="follow_ups")
    op.drop_index("ix_follow_ups_tenant_id_status", table_name="follow_ups")
    op.drop_index("ix_follow_ups_tenant_id", table_name="follow_ups")
    op.drop_table("follow_ups")

    # actor_kind is not dropped: migration 0008 created it and still uses it.
    FOLLOW_UP_STATUS.drop(op.get_bind(), checkfirst=False)
