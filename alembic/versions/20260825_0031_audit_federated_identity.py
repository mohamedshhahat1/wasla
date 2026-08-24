"""Add the federated identity actions to the audit vocabulary.

Revision ID: 0031
Revises: 0030

Same shape as 0029, for the same reasons. `ALTER TYPE ... ADD VALUE` cannot run
inside a transaction block, so each statement goes in an autocommit block, and
`IF NOT EXISTS` makes a partially applied run repeatable - which is the state
autocommit can leave behind when five separate statements are not one unit.
"""

from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_NEW_ACTIONS = (
    "google_login_succeeded",
    "google_login_failed",
    "google_identity_linked",
    "google_identity_link_failed",
    "google_identity_unlinked",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for action in _NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")


def downgrade() -> None:
    """Deliberately empty.

    PostgreSQL cannot remove a value from an enum. Doing it properly means
    creating a replacement type, rewriting every `audit_logs` row onto it,
    swapping the column and dropping the old type - a full rewrite of the one
    table in this schema that only ever grows, in order to remove five labels
    that are inert the moment nothing emits them.

    Migrations 0025 and 0029 took the same position.
    """
