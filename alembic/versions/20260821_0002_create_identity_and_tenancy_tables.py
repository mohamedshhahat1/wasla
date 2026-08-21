"""create identity and tenancy tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-21

Creates the tenancy foundation: tenants, global users, the memberships that
join them, and tenant invitations.

Enum types are created and dropped explicitly. Dropping a table does not drop
the types its columns used, so a downgrade that skipped this would leave the
database unable to run the upgrade again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# create_type=False: these are created once, explicitly, in upgrade().
TENANT_STATUS = postgresql.ENUM(
    "active",
    "suspended",
    name="tenant_status",
    create_type=False,
)
PLATFORM_ROLE = postgresql.ENUM(
    "platform_owner",
    "platform_admin",
    name="platform_role",
    create_type=False,
)
TENANT_ROLE = postgresql.ENUM(
    "tenant_owner",
    "tenant_admin",
    "member",
    name="tenant_role",
    create_type=False,
)
INVITATION_STATUS = postgresql.ENUM(
    "pending",
    "accepted",
    "revoked",
    "expired",
    name="invitation_status",
    create_type=False,
)
ENUM_TYPES = (TENANT_STATUS, PLATFORM_ROLE, TENANT_ROLE, INVITATION_STATUS)


def _audit_columns():
    """created_at / updated_at, matching TimestampMixin."""
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
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("status", TENANT_STATUS, nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("platform_role", PLATFORM_ROLE, nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", TENANT_ROLE, nullable=False),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_memberships_user_id_tenant_id"),
    )
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"], unique=False)
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"], unique=False)

    op.create_table(
        "tenant_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", TENANT_ROLE, nullable=False),
        sa.Column("status", INVITATION_STATUS, nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_invitations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name=op.f("fk_tenant_invitations_invited_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_invitations")),
        sa.UniqueConstraint("token_hash", name="uq_tenant_invitations_token_hash"),
    )
    op.create_index(
        "ix_tenant_invitations_tenant_id",
        "tenant_invitations",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_invitations_tenant_id_email",
        "tenant_invitations",
        ["tenant_id", "email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_invitations_tenant_id_email", table_name="tenant_invitations")
    op.drop_index("ix_tenant_invitations_tenant_id", table_name="tenant_invitations")
    op.drop_table("tenant_invitations")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_index("ix_memberships_tenant_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("tenants")

    bind = op.get_bind()
    for enum_type in reversed(ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
