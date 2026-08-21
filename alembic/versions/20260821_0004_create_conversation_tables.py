"""create contact, conversation and message tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# create_type=False: the types are created explicitly below, so table creation
# does not try to create them a second time.
CONVERSATION_STATUS = postgresql.ENUM(
    "open",
    "pending",
    "closed",
    name="conversation_status",
    create_type=False,
)
CONVERSATION_MODE = postgresql.ENUM(
    "ai",
    "human",
    name="conversation_mode",
    create_type=False,
)
MESSAGE_DIRECTION = postgresql.ENUM(
    "inbound",
    "outbound",
    name="message_direction",
    create_type=False,
)
MESSAGE_KIND = postgresql.ENUM(
    "text",
    "image",
    "document",
    "audio",
    "video",
    "location",
    "interactive",
    "unsupported",
    name="message_kind",
    create_type=False,
)
MESSAGE_STATUS = postgresql.ENUM(
    "received",
    "pending",
    "sent",
    "delivered",
    "read",
    "failed",
    name="message_status",
    create_type=False,
)
ENUM_TYPES = (
    CONVERSATION_STATUS,
    CONVERSATION_MODE,
    MESSAGE_DIRECTION,
    MESSAGE_KIND,
    MESSAGE_STATUS,
)


def _audit_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_contacts_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "wa_id", name="uq_contacts_tenant_id_wa_id"),
    )
    op.create_index("ix_contacts_tenant_id", "contacts", ["tenant_id"], unique=False)

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", CONVERSATION_STATUS, nullable=False),
        sa.Column("mode", CONVERSATION_MODE, nullable=False),
        sa.Column("assigned_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("handoff_reason", sa.String(length=200), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_conversations_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_conversations_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
            name="fk_conversations_account_id_whatsapp_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_conversations_assigned_to_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "contact_id",
            "account_id",
            name="uq_conversations_tenant_id_contact_id_account_id",
        ),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"], unique=False)
    op.create_index(
        "ix_conversations_tenant_id_status",
        "conversations",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_tenant_id_last_message_at",
        "conversations",
        ["tenant_id", "last_message_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_contact_id",
        "conversations",
        ["contact_id"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wa_message_id", sa.String(length=255), nullable=True),
        sa.Column("direction", MESSAGE_DIRECTION, nullable=False),
        sa.Column("kind", MESSAGE_KIND, nullable=False),
        sa.Column("status", MESSAGE_STATUS, nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("sent_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_messages_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sent_by_id"],
            ["users.id"],
            name="fk_messages_sent_by_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "wa_message_id",
            name="uq_messages_tenant_id_wa_message_id",
        ),
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"], unique=False)
    op.create_index(
        "ix_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_id_created_at", table_name="messages")
    op.drop_index("ix_messages_tenant_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_conversations_contact_id", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id_last_message_at", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id_status", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_contacts_tenant_id", table_name="contacts")
    op.drop_table("contacts")

    bind = op.get_bind()
    for enum_type in reversed(ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
