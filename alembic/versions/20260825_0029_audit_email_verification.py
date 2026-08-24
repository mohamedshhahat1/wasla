"""Add the email verification actions to the audit vocabulary.

Revision ID: 0029
Revises: 0028

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, so each one
goes in an autocommit block - the same shape as the earlier revisions that
extended this enum.

`IF NOT EXISTS` so a partially applied run can be repeated safely, which is the
situation autocommit creates: an ordinary migration either happens or does not,
but three separate autocommitted statements can leave one or two of them
done.
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_NEW_ACTIONS = (
    "email_verification_requested",
    "email_verified",
    "email_verification_failed",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for action in _NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")


def downgrade() -> None:
    """Deliberately empty.

    PostgreSQL cannot remove a value from an enum. Doing it properly means
    creating a replacement type, rewriting every `audit_logs` row onto it,
    swapping the column and dropping the old type - a full table rewrite of the
    one table in this schema that only ever grows, to remove three labels that
    are inert when nothing emits them.

    An unused enum value costs nothing, so this downgrade leaves them. Migration
    0025 took the same position for the same reason.
    """
