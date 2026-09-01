"""Give the billing lifecycle somewhere for an unpaid subscription to end up.

Revision ID: 0037
Revises: 0036

Two enums grow by one label each. `subscription_status` gains `suspended`,
which is where `past_due` goes when nobody ever pays (ADR-061), and
`audit_action` gains `subscription_suspended` so the trail can say the platform
stopped serving rather than that the customer left.

Same shape as 0025, 0029, 0034 and 0036, for the same reason: `ALTER TYPE ...
ADD VALUE` cannot run inside a transaction block, so each statement goes in an
autocommit block, and `IF NOT EXISTS` makes a partially applied run repeatable -
which is the state autocommit can leave behind when two separate statements are
not one unit.

No column, no index and no data change. `suspended` is a value nothing holds
until the billing worker first writes one.
"""

from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE subscription_status ADD VALUE IF NOT EXISTS 'suspended'")
        op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'subscription_suspended'")


def downgrade() -> None:
    """Deliberately empty.

    PostgreSQL cannot remove a value from an enum. Doing it properly means
    creating a replacement type, rewriting every row that uses the old one,
    swapping the column and dropping the old type - and for `audit_action` that
    is a full rewrite of the one table in this schema that only ever grows.

    For `subscription_status` there is a second reason to leave it: a row that
    reached `suspended` describes a workspace the platform stopped serving, and
    a downgrade that had to rewrite those rows would have to invent a status
    for them. Refusing to guess is better than choosing wrongly.

    Migrations 0025, 0029, 0034 and 0036 took the same position.
    """
