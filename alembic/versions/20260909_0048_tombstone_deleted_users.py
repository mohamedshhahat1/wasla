"""make historical soft-deleted users inert

Authentication now excludes every row whose ``deleted_at`` is set. This data
repair also establishes the redundant persisted invariant for rows that may
have been soft-deleted while still active: deactivate them and invalidate all
credentials minted under their current token version.

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE users
        SET is_active = false,
            token_version = token_version + 1
        WHERE deleted_at IS NOT NULL
          AND is_active = true
        """)


def downgrade() -> None:
    """Do not resurrect deleted identities or previously invalidated tokens."""
