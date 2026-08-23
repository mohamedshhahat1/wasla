"""add the audit action for re-proving a number already held

ADR-041. Numbers claimed before ADR-037 carry no `ownership_verified_at`, and
until now there was no way to give them one: `connect` refuses a number that is
already claimed, so the only route was to release the number and claim it again
- which frees it to the entire platform in between and hands an attacker a race
worth running. The safe-looking action was the dangerous one.

`POST /whatsapp/accounts/{id}/verify` closes that, and this is the audit action
it writes. Schema-only, and one value: the timestamp column it stamps already
exists from 0022.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the label
stays behind. Recreating `audit_action` would mean rewriting every row in
`audit_logs`, which is a far riskier operation than carrying one unused value.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `IF NOT EXISTS` makes it re-runnable
    # after a partial failure, which matters because this is not covered by the
    # surrounding transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'whatsapp_account_verified'")


def downgrade() -> None:
    """Nothing to undo. See the module docstring."""
