"""add the audit action for a plan grant withdrawn by a reversal

ADR-096. A full refund now takes back the plan the payment bought, and that
transition needs its own name in the trail. `subscription_plan_changed` already
covers a customer choosing a plan; reusing it here would record the platform
withdrawing a grant as though the workspace had asked to downgrade, and the one
question an operator asks about a surprise downgrade is which of those it was.

Schema-only, one value.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the
label stays behind. There is nothing else to undo.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

VALUES = ("subscription_plan_withdrawn",)


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
    than carrying one unused value.
    """
