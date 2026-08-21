"""create agent tables

Revision ID: 0005
Revises: 0004

Creates the enum type explicitly before the tables and drops it on downgrade.
CI runs upgrade, downgrade to base, then upgrade again, so a type left behind
by downgrade would fail the second upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

AGENT_STATUS = postgresql.ENUM(
    "draft",
    "active",
    "disabled",
    name="agent_status",
    create_type=False,
)


def _audit_columns():
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


def upgrade():
    bind = op.get_bind()
    AGENT_STATUS.create(bind, checkfirst=False)

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", AGENT_STATUS, nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("memory_message_limit", sa.Integer(), nullable=False),
        sa.Column("memory_token_budget", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_agents_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_id_name"),
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"], unique=False)
    op.create_index(
        "ix_agents_tenant_id_status",
        "agents",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "agent_tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_tools_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_agent_tools_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tools"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_id",
            "name",
            name="uq_agent_tools_tenant_id_agent_id_name",
        ),
    )
    op.create_index("ix_agent_tools_tenant_id", "agent_tools", ["tenant_id"], unique=False)
    op.create_index("ix_agent_tools_agent_id", "agent_tools", ["agent_id"], unique=False)


def downgrade():
    op.drop_index("ix_agent_tools_agent_id", table_name="agent_tools")
    op.drop_index("ix_agent_tools_tenant_id", table_name="agent_tools")
    op.drop_table("agent_tools")
    op.drop_index("ix_agents_tenant_id_status", table_name="agents")
    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_table("agents")
    AGENT_STATUS.drop(op.get_bind(), checkfirst=False)
