"""create whatsapp account and event tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# create_type=False: the types are created explicitly below, so table creation
# does not try to create them a second time.
ACCOUNT_STATUS = postgresql.ENUM(
    "active",
    "disabled",
    name="whatsapp_account_status",
    create_type=False,
)
EVENT_KIND = postgresql.ENUM(
    "message",
    "status",
    "unsupported",
    name="whatsapp_event_kind",
    create_type=False,
)
EVENT_STATE = postgresql.ENUM(
    "received",
    "processed",
    "failed",
    name="whatsapp_event_state",
    create_type=False,
)
ENUM_TYPES = (ACCOUNT_STATUS, EVENT_KIND, EVENT_STATE)


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
        "whatsapp_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_number_id", sa.String(length=64), nullable=False),
        sa.Column("waba_id", sa.String(length=64), nullable=False),
        sa.Column("display_phone_number", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("status", ACCOUNT_STATUS, nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_accounts")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_whatsapp_accounts_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("phone_number_id", name="uq_whatsapp_accounts_phone_number_id"),
    )
    op.create_index(
        "ix_whatsapp_accounts_tenant_id",
        "whatsapp_accounts",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "whatsapp_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("kind", EVENT_KIND, nullable=False),
        sa.Column("state", EVENT_STATE, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_events")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_whatsapp_events_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
            name="fk_whatsapp_events_account_id_whatsapp_accounts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_whatsapp_events_tenant_id_event_id",
        ),
    )
    op.create_index("ix_whatsapp_events_tenant_id", "whatsapp_events", ["tenant_id"], unique=False)
    op.create_index(
        "ix_whatsapp_events_account_id",
        "whatsapp_events",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_whatsapp_events_tenant_id_state",
        "whatsapp_events",
        ["tenant_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_events_tenant_id_state", table_name="whatsapp_events")
    op.drop_index("ix_whatsapp_events_account_id", table_name="whatsapp_events")
    op.drop_index("ix_whatsapp_events_tenant_id", table_name="whatsapp_events")
    op.drop_table("whatsapp_events")

    op.drop_index("ix_whatsapp_accounts_tenant_id", table_name="whatsapp_accounts")
    op.drop_table("whatsapp_accounts")

    bind = op.get_bind()
    for enum_type in reversed(ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
