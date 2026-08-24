"""create the email outbox and suppression tables

ADR-042. Email is delivered through a transactional outbox: the domain action
that decides an email should exist writes a row to `email_messages` in its own
transaction, and the email worker claims and sends it afterwards. The unique
constraint on `idempotency_key` is what makes a retried request or a sweep
that ran twice produce one email rather than two - the guarantee is the
constraint, not the callers' discipline.

`email_suppressions` is written by the provider webhook on a hard bounce or a
complaint and consulted by the worker before every send, so an unreachable
address is not retried into a sender-reputation problem.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

EMAIL_STATUS = postgresql.ENUM(
    "pending",
    "sending",
    "sent",
    "delivered",
    "failed",
    name="email_status",
    create_type=False,
)


def upgrade() -> None:
    EMAIL_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "email_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("template", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", EMAIL_STATUS, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_message", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_email_messages_idempotency_key"),
    )
    op.create_index(
        "ix_email_messages_status_available_at",
        "email_messages",
        ["status", "available_at"],
    )
    op.create_index("ix_email_messages_tenant_id", "email_messages", ["tenant_id"])
    op.create_index(
        "ix_email_messages_provider_message_id",
        "email_messages",
        ["provider_message_id"],
    )

    op.create_table(
        "email_suppressions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("recipient", name="uq_email_suppressions_recipient"),
    )


def downgrade() -> None:
    op.drop_table("email_suppressions")
    op.drop_table("email_messages")
    sa.Enum(name="email_status").drop(op.get_bind(), checkfirst=True)
