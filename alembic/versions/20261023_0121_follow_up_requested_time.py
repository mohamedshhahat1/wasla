"""A follow-up keeps the time it was asked for, and says why it waits.

Revision ID: 0121
Revises: 0120

FQH-04, FLIM-01 (ADR-152, ADR-153). A follow-up that waits - for Meta's
portfolio limit at 100 %, or for the workspace's quiet hours - used to have its
`scheduled_at` overwritten, so the time a person or the AI asked for was lost.

1. `follow_ups.requested_at`: the time asked for, set once at scheduling and
   never changed by a wait. Backfilled from `scheduled_at` in batches of
   5,000 rows, each its own transaction; then made NOT NULL through a
   validated CHECK, which `SET NOT NULL` uses instead of scanning, and the
   CHECK is dropped.
2. `follow_ups.deferred_reason` (`ck_follow_ups_deferred_reason_known`:
   portfolio_limit, quiet_hours): why it waits now, or null.
3. `follow_ups.deferrals` (JSONB, null): every wait in order,
   `[{reason, until, at}]`, so a nudge that waited for the night and then for
   the limit records both.

**Online.** The new columns are nullable without a default (catalogue only);
the backfill touches each row once in short transactions; every CHECK is added
`NOT VALID` and validated under a bounded `lock_timeout`, holding only
`SHARE UPDATE EXCLUSIVE` while it scans. No table rewrite.

**Downgrade refuses** while any follow-up's requested time differs from its
scheduled time, or any records a wait: each would be lost. Otherwise it drops
exactly what the upgrade added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
BATCH = 5000
REASONS = ("portfolio_limit", "quiet_hours")
REASON_CHECK = "ck_follow_ups_deferred_reason_known"
FILLED_CHECK = "ck_follow_ups_requested_at_filled"
RUNBOOK = "docs/RUNBOOK.md, 'Follow-ups, header media and realtime (0121-0125)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "follow_ups", sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("follow_ups", sa.Column("deferred_reason", sa.String(32), nullable=True))
    op.add_column(
        "follow_ups",
        sa.Column("deferrals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute("RESET lock_timeout")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while True:
            updated = bind.exec_driver_sql(
                "UPDATE follow_ups SET requested_at = scheduled_at WHERE id IN"  # noqa: S608 - module constant
                f" (SELECT id FROM follow_ups WHERE requested_at IS NULL LIMIT {BATCH})"
            ).rowcount
            if not updated:
                break

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    # A row written by the old code between the batches and here: filled now.
    op.execute("UPDATE follow_ups SET requested_at = scheduled_at WHERE requested_at IS NULL")
    op.execute(
        f"ALTER TABLE follow_ups ADD CONSTRAINT {FILLED_CHECK}"
        " CHECK (requested_at IS NOT NULL) NOT VALID"
    )
    allowed = ", ".join(f"'{reason}'" for reason in REASONS)
    op.execute(
        f"ALTER TABLE follow_ups ADD CONSTRAINT {REASON_CHECK}"
        f" CHECK (deferred_reason IS NULL OR deferred_reason IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE follow_ups VALIDATE CONSTRAINT {FILLED_CHECK}")
    op.execute(f"ALTER TABLE follow_ups VALIDATE CONSTRAINT {REASON_CHECK}")
    op.execute("ALTER TABLE follow_ups ALTER COLUMN requested_at SET NOT NULL")
    op.execute(f"ALTER TABLE follow_ups DROP CONSTRAINT {FILLED_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    moved = bind.exec_driver_sql(
        "SELECT count(*) FROM follow_ups WHERE requested_at IS DISTINCT FROM scheduled_at"
    ).scalar_one()
    if moved:
        found.append(f"follow-ups sent or due later than the time asked for: {moved}")
    waited = bind.exec_driver_sql(
        "SELECT count(*) FROM follow_ups WHERE deferred_reason IS NOT NULL OR deferrals IS NOT NULL"
    ).scalar_one()
    if waited:
        found.append(f"follow-ups recording a wait: {waited}")
    if found:
        raise RuntimeError(
            f"Follow-up requested times cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE follow_ups DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.drop_column("follow_ups", "deferrals")
    op.drop_column("follow_ups", "deferred_reason")
    op.drop_column("follow_ups", "requested_at")
