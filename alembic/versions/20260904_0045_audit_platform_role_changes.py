"""add the audit actions for granting and revoking a platform role

ADR-094. `users.platform_role` had no code path that wrote it, so the only way
to create the first platform administrator was an `UPDATE` somebody typed - a
step nobody had written down and which left no trace of who took authority over
every workspace on the platform. There is a supported operator command now, and
these are the two entries it writes.

Schema-only, and two values. The entry names the account, the role it was given
and the role it replaced. It carries no credential and no session, because an
audit log read by people should not be a second copy of anything secret.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the
labels stay behind. There is nothing else to undo.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

VALUES = ("platform_role_granted", "platform_role_revoked")


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `IF NOT EXISTS` makes it re-runnable
    # after a partial failure, which matters because this is not covered by the
    # surrounding transaction.
    with op.get_context().autocommit_block():
        for value in VALUES:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Nothing to undo.

    An enum label cannot be dropped in PostgreSQL, and recreating `audit_action`
    would mean rewriting every row in `audit_logs` - a far riskier operation
    than carrying two unused values.
    """
