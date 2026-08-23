"""create the analytics event table

Revision ID: 0015
Revises: 0014

One table, deliberately small (ADR-028). Almost every figure a dashboard shows
is already a row somewhere - messages, leads, sentiment readings, campaign
recipients - and a second copy of those would be two shapes to migrate and two
answers to the same question.

What no other table records is a handoff. `conversations.mode` says where a
conversation is now; it cannot say when it moved, how often, or who decided it.

Indexes match the three reads: a workspace over a window, one event type over a
window, and the history of a single conversation. `actor_id` is `SET NULL` on
delete rather than `CASCADE`: the handoff still happened after the colleague who
made it has left, and deleting their account must not rewrite last quarter's
numbers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

ANALYTICS_EVENT_TYPE = postgresql.ENUM(
    "handoff",
    "handoff_resumed",
    name="analytics_event_type",
    create_type=False,
)
ANALYTICS_SOURCE = postgresql.ENUM(
    "agent",
    "sentiment",
    "user",
    "system",
    name="analytics_source",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    ANALYTICS_EVENT_TYPE.create(bind, checkfirst=False)
    ANALYTICS_SOURCE.create(bind, checkfirst=False)

    op.create_table(
        "analytics_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", ANALYTICS_EVENT_TYPE, nullable=False),
        sa.Column("source", ANALYTICS_SOURCE, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            name="fk_analytics_events_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_analytics_events_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_analytics_events_actor_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analytics_events"),
    )
    op.create_index("ix_analytics_events_tenant_id", "analytics_events", ["tenant_id"])
    op.create_index(
        "ix_analytics_events_tenant_id_occurred_at",
        "analytics_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_analytics_events_tenant_id_event_type_occurred_at",
        "analytics_events",
        ["tenant_id", "event_type", "occurred_at"],
    )
    op.create_index(
        "ix_analytics_events_conversation_id",
        "analytics_events",
        ["conversation_id"],
    )


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_analytics_events_conversation_id", table_name="analytics_events")
    op.drop_index(
        "ix_analytics_events_tenant_id_event_type_occurred_at",
        table_name="analytics_events",
    )
    op.drop_index("ix_analytics_events_tenant_id_occurred_at", table_name="analytics_events")
    op.drop_index("ix_analytics_events_tenant_id", table_name="analytics_events")
    op.drop_table("analytics_events")

    ANALYTICS_SOURCE.drop(bind, checkfirst=False)
    ANALYTICS_EVENT_TYPE.drop(bind, checkfirst=False)
