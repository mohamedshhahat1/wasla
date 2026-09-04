"""add the audit actions for platform staff reading across workspaces

ADR-095. Every platform *write* was recorded - a payment, a voided invoice, a
disabled account - and no platform *read* was. That asymmetry was defensible
while the reads were aggregates, and it stops being defensible the moment
somebody asks "who looked at our workspace".

Schema-only, and three values. The entries name the actor, the class of data
reached, and the workspace when one is named. They deliberately carry no search
string, no workspace name, no address and no customer content: the point of a
privacy trail is to record who accessed what kind of thing, not to become a
second copy of it.

Downgrade note: PostgreSQL cannot remove a value from an enum type, so the
labels stay behind. There is nothing else to undo.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

VALUES = (
    "platform_overview_read",
    "platform_workspaces_read",
    "platform_audit_log_read",
)


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
    than carrying three unused values.
    """
