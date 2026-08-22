"""create the message media table

Revision ID: 0010
Revises: 0009

One row per message, and the unique constraint on `message_id` is what says so.
WhatsApp sends a single attachment per message, so a second row could only come
from a replay - and it is that constraint, not a check in the service, that
makes the download job safe to retry.

`ix_message_media_conversation_id` is not an optimisation either. Before an
agent is allowed to answer, the worker asks whether anything on the conversation
is still unread, and that question is asked once per media message that arrives.

Both foreign keys cascade. A media row describes one message in one
conversation and has no meaning once either is gone; the file in storage is
removed separately, because a delete inside a transaction cannot be rolled back
on disk.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

MEDIA_STATUS = postgresql.ENUM(
    "pending",
    "downloading",
    "stored",
    "ready",
    "skipped",
    "failed",
    name="media_status",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    MEDIA_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "message_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_media_id", sa.String(length=255), nullable=True),
        sa.Column("status", MEDIA_STATUS, nullable=False),
        sa.Column("mime_type", sa.String(length=150), nullable=True),
        sa.Column("filename", sa.String(length=300), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=500), nullable=True),
        sa.Column("is_voice", sa.Boolean(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_message_media_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_message_media_message_id_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_message_media_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_media"),
        sa.UniqueConstraint("message_id", name="uq_message_media_message_id"),
    )

    op.create_index("ix_message_media_tenant_id", "message_media", ["tenant_id"])
    op.create_index("ix_message_media_tenant_id_status", "message_media", ["tenant_id", "status"])
    op.create_index("ix_message_media_conversation_id", "message_media", ["conversation_id"])


def downgrade():
    op.drop_index("ix_message_media_conversation_id", table_name="message_media")
    op.drop_index("ix_message_media_tenant_id_status", table_name="message_media")
    op.drop_index("ix_message_media_tenant_id", table_name="message_media")
    op.drop_table("message_media")
    MEDIA_STATUS.drop(op.get_bind(), checkfirst=False)
