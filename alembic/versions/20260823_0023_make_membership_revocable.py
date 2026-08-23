"""make a workspace membership revocable

ADR-038. A workspace could invite people and could not remove them: there was no
status on `memberships` and no members router at all.

`server_default='active'` rather than a backfill. It gives every existing row
the right value with no data migration, and it means a row inserted by something
that does not know about the column — a fixture, a support script — is active
rather than null, which is the only safe direction for a column that authorises
things.

`revoked_by_id` is `ON DELETE SET NULL`, never `CASCADE`: deleting the
administrator who removed somebody must not delete the record of the removal.

The `(tenant_id, status)` index is read by every authorization decision in the
product, on every request — `get_active_workspace` resolves the membership each
time rather than trusting the token, which is what makes revocation immediate.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the three
audit actions added here stay behind. Recreating `audit_action` and rewriting
every log row is a far riskier operation than tolerating three unused labels.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

MEMBERSHIP_STATUS = "membership_status"
NEW_AUDIT_ACTIONS = ("member_removed", "member_left", "member_reinstated")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")

    membership_status = sa.Enum("active", "revoked", name=MEMBERSHIP_STATUS)
    membership_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "memberships",
        sa.Column("status", membership_status, nullable=False, server_default="active"),
    )
    op.add_column(
        "memberships",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memberships",
        sa.Column("revoked_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_memberships_revoked_by_id_users",
        "memberships",
        "users",
        ["revoked_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_memberships_tenant_id_status",
        "memberships",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_memberships_tenant_id_status", table_name="memberships")
    op.drop_constraint("fk_memberships_revoked_by_id_users", "memberships", type_="foreignkey")
    op.drop_column("memberships", "revoked_by_id")
    op.drop_column("memberships", "revoked_at")
    op.drop_column("memberships", "status")
    sa.Enum(name=MEMBERSHIP_STATUS).drop(op.get_bind(), checkfirst=True)
