"""add the audit action for a replayed refresh token

ADR-039. A refresh token presented twice is either a leak being replayed or a
client bug, and the platform's response is to invalidate every token the account
holds. That is the most security-relevant thing this system does on its own
initiative, so it is recorded.

Schema-only, and one value. The entry itself names the account and the version
the teardown raised it to; the token that was replayed is identified nowhere,
because an audit log is read by people and a log of credentials is a second copy
of them.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the label
stays behind. There is nothing else to undo.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `IF NOT EXISTS` makes it re-runnable
    # after a partial failure, which matters because this is not covered by the
    # surrounding transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'refresh_token_reused'")


def downgrade() -> None:
    """Nothing to undo.

    An enum label cannot be dropped in PostgreSQL, and recreating `audit_action`
    would mean rewriting every row in `audit_logs` — a far riskier operation
    than carrying one unused value.
    """
