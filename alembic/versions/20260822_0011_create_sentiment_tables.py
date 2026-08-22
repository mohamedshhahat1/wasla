"""create the sentiment tables and escalation columns

Revision ID: 0011
Revises: 0010

One row per analysed message, and the unique constraint on `message_id` is what
makes the analysis safe to retry. An agent job that fails after the reading was
taken must not pay for a second inference when it runs again, and it is this
constraint rather than a check in a service that guarantees it.

`conversations` gains the current reading. That is duplication, on purpose: the
inbox sorts and filters on priority, and asking "which conversations need
attention" must not become a scan of every reading ever taken.

`agents.escalation_sentiment` is created with a server default of `angry` rather
than null, so agents that already exist start escalating rather than silently
opting out of a feature they were never asked about. A workspace that does not
want automatic handoff sets the column to null explicitly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SENTIMENT_LABEL = postgresql.ENUM(
    "positive",
    "neutral",
    "negative",
    "angry",
    name="sentiment_label",
    create_type=False,
)
CONVERSATION_PRIORITY = postgresql.ENUM(
    "normal",
    "high",
    "urgent",
    name="conversation_priority",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    SENTIMENT_LABEL.create(bind, checkfirst=False)
    CONVERSATION_PRIORITY.create(bind, checkfirst=False)

    op.create_table(
        "message_sentiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", SENTIMENT_LABEL, nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("intent", sa.String(length=120), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("escalated", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
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
            name="fk_message_sentiments_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_message_sentiments_message_id_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_message_sentiments_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_sentiments"),
        sa.UniqueConstraint("message_id", name="uq_message_sentiments_message_id"),
    )
    op.create_index(
        "ix_message_sentiments_tenant_id",
        "message_sentiments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_message_sentiments_tenant_id_label",
        "message_sentiments",
        ["tenant_id", "label"],
    )
    op.create_index(
        "ix_message_sentiments_conversation_id",
        "message_sentiments",
        ["conversation_id"],
    )

    op.add_column("conversations", sa.Column("sentiment", SENTIMENT_LABEL, nullable=True))
    op.add_column("conversations", sa.Column("sentiment_score", sa.Float(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column(
            "priority",
            CONVERSATION_PRIORITY,
            nullable=False,
            server_default="normal",
        ),
    )
    op.add_column("conversations", sa.Column("intent", sa.String(length=120), nullable=True))
    op.add_column("conversations", sa.Column("intent_confidence", sa.Float(), nullable=True))
    op.create_index(
        "ix_conversations_tenant_id_priority",
        "conversations",
        ["tenant_id", "priority"],
    )

    op.add_column(
        "agents",
        sa.Column(
            "escalation_sentiment",
            SENTIMENT_LABEL,
            nullable=True,
            server_default="angry",
        ),
    )


def downgrade():
    bind = op.get_bind()

    op.drop_column("agents", "escalation_sentiment")

    op.drop_index("ix_conversations_tenant_id_priority", table_name="conversations")
    op.drop_column("conversations", "intent_confidence")
    op.drop_column("conversations", "intent")
    op.drop_column("conversations", "priority")
    op.drop_column("conversations", "sentiment_score")
    op.drop_column("conversations", "sentiment")

    op.drop_index("ix_message_sentiments_conversation_id", table_name="message_sentiments")
    op.drop_index("ix_message_sentiments_tenant_id_label", table_name="message_sentiments")
    op.drop_index("ix_message_sentiments_tenant_id", table_name="message_sentiments")
    op.drop_table("message_sentiments")

    CONVERSATION_PRIORITY.drop(bind, checkfirst=False)
    SENTIMENT_LABEL.drop(bind, checkfirst=False)
