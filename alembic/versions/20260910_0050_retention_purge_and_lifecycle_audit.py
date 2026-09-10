"""give a tombstoned workspace a retention deadline, and audit the rest of the lifecycle

Revision ID: 0050
Revises: 0049

Two independent changes that ship together because they are one feature.

**Retention.** `tenants` gains `purge_due_at` and `purged_at`. Deletion has
been a tombstone - access stops, data stays - which is a good access-control
mechanism and is not erasure, and saying otherwise is the claim
docs/SECURITY.md exists to avoid. These two columns are what turn it into a
lifecycle: the first says when the operational data becomes eligible to be
erased, the second says when it was, and the second is what makes the sweep
idempotent.

`purge_due_at` is **stored rather than computed** from `deleted_at` plus the
configured retention. The window promised to a customer is the one that was in
force the day they left; an operator shortening `WORKSPACE_DELETION_RETENTION_DAYS`
must not retroactively bring forward the erasure of data already tombstoned,
and a computed deadline would do exactly that.

The partial index is the sweep's only query - tombstoned, due, not yet purged -
and is partial because those rows are a vanishing fraction of the table and stay
that way.

**The backfill** gives existing tombstones a deadline. Without it, a workspace
deleted before this migration would have `purge_due_at IS NULL` for ever and be
invisible to the sweep - retained indefinitely by accident rather than by
policy. The interval is written into the migration rather than read from
settings, for the reason above: it is the window those workspaces were deleted
under, and it must not move when configuration does.

**Audit vocabulary.** Three labels for acts that had none:
`workspace_ownership_repaired` (platform staff putting an ownerless workspace
back under somebody's control), `workspace_purged` (the erasure itself), and
`account_reauthenticated` (proving a linked Google identity to authorise a
high-risk action).

Additive in both halves. The columns are nullable with no default, so the
migration is safe to apply ahead of the code that writes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

NEW_ACTIONS = (
    "workspace_ownership_repaired",
    "workspace_purged",
    "account_reauthenticated",
)

PURGE_INDEX = "ix_tenants_purge_due"

# The retention window existing tombstones are granted, in days. A literal, not
# a setting: see the module docstring.
BACKFILL_RETENTION_DAYS = 30


def upgrade() -> None:
    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `IF NOT EXISTS` makes it re-runnable
    # after a partial failure, which matters because this is not covered by the
    # surrounding transaction.
    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column(
        "tenants",
        sa.Column("purge_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        PURGE_INDEX,
        "tenants",
        ["purge_due_at"],
        postgresql_where=sa.text(
            "deleted_at IS NOT NULL AND purged_at IS NULL AND purge_due_at IS NOT NULL"
        ),
    )

    # Existing tombstones get a deadline measured from when they were deleted,
    # so a workspace closed two months ago is already eligible rather than
    # being granted a fresh window it never asked for.
    # The interval is a module constant above, never caller input - but it is
    # still bound rather than interpolated, because `make_interval` takes a
    # parameter and a formatted SQL string in a migration is a habit worth not
    # having.
    op.execute(sa.text("""
            UPDATE tenants
            SET purge_due_at = deleted_at + make_interval(days => :days)
            WHERE deleted_at IS NOT NULL
              AND purge_due_at IS NULL
            """).bindparams(days=BACKFILL_RETENTION_DAYS))


def downgrade() -> None:
    """Drops the columns; leaves the enum labels.

    An enum label cannot be removed in PostgreSQL without recreating the type
    and rewriting every `audit_logs` row onto it - a full rewrite of the one
    table in this schema that only ever grows, to remove three labels that are
    inert the moment nothing emits them. Migrations 0025, 0029, 0034, 0036,
    0045, 0046, 0047 and 0049 all took this position.

    The columns do come back out, and doing so loses the record of which
    workspaces had already been purged. That is acceptable only because a
    downgrade is a development operation: on a database where anything has
    actually been erased, the erasure is not undone by dropping the column that
    recorded it, and re-upgrading would grant those workspaces a fresh window.
    """
    op.drop_index(PURGE_INDEX, table_name="tenants")
    op.drop_column("tenants", "purged_at")
    op.drop_column("tenants", "purge_due_at")
