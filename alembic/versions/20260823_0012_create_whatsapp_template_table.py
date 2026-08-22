"""create the whatsapp template registry

Revision ID: 0012
Revises: 0011

A mirror of what Meta says about a workspace's approved templates. Nothing in
Wasla writes these rows except the sync, and nothing approves a template here —
approval happens in the WhatsApp Business Manager.

Identity is `(tenant_id, account_id, name, language)`. The account is part of it
because two numbers in one workspace can belong to different WhatsApp Business
accounts with genuinely different template sets, and the name and language pair
is what a send actually addresses.

`status` and `category` both carry an `unknown` member. A status or category
Meta introduces after this migration must land somewhere that is not sendable,
rather than being guessed into one that is.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TEMPLATE_CATEGORY = postgresql.ENUM(
    "marketing",
    "utility",
    "authentication",
    "unknown",
    name="template_category",
    create_type=False,
)
TEMPLATE_STATUS = postgresql.ENUM(
    "approved",
    "pending",
    "rejected",
    "paused",
    "disabled",
    "unknown",
    name="template_status",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    TEMPLATE_CATEGORY.create(bind, checkfirst=False)
    TEMPLATE_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "whatsapp_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meta_template_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("category", TEMPLATE_CATEGORY, nullable=False),
        sa.Column("status", TEMPLATE_STATUS, nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("variable_count", sa.Integer(), nullable=False),
        sa.Column("quality_rating", sa.String(length=16), nullable=True),
        sa.Column("rejection_reason", sa.String(length=200), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_whatsapp_templates_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
            name="fk_whatsapp_templates_account_id_whatsapp_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_templates"),
        sa.UniqueConstraint(
            "tenant_id",
            "account_id",
            "name",
            "language",
            name="uq_whatsapp_templates_tenant_id_account_id_name_language",
        ),
    )
    op.create_index(
        "ix_whatsapp_templates_tenant_id",
        "whatsapp_templates",
        ["tenant_id"],
    )
    op.create_index(
        "ix_whatsapp_templates_tenant_id_status",
        "whatsapp_templates",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_whatsapp_templates_account_id",
        "whatsapp_templates",
        ["account_id"],
    )


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_whatsapp_templates_account_id", table_name="whatsapp_templates")
    op.drop_index("ix_whatsapp_templates_tenant_id_status", table_name="whatsapp_templates")
    op.drop_index("ix_whatsapp_templates_tenant_id", table_name="whatsapp_templates")
    op.drop_table("whatsapp_templates")

    TEMPLATE_STATUS.drop(bind, checkfirst=False)
    TEMPLATE_CATEGORY.drop(bind, checkfirst=False)
