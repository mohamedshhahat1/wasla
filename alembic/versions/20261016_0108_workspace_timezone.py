"""Every workspace has a time zone.

Revision ID: 0108
Revises: 0107

TZ-01 (ADR-145). `tenants.timezone`, an IANA zone name, required: chosen at
onboarding, and the zone quiet hours, delivery plans and every local time a
campaign shows are computed in.

1. `tenants.timezone` is added nullable, without a default: catalogue-only.
2. Every existing workspace is given `Africa/Cairo` - every workspace that
   exists at this revision is Egyptian - a batch of ids at a time, in id order
   (0083's keyset walk), filling only what is still empty.
3. `NOT NULL` through a validated CHECK (0084's pattern): the CHECK is added
   `NOT VALID` and validated under a bounded `lock_timeout`, holding only
   `SHARE UPDATE EXCLUSIVE` while it scans; `SET NOT NULL` then trusts it
   rather than scanning again under an exclusive lock; the CHECK is dropped.

No server default, deliberately: a workspace created without a decision is the
thing TZ-01 forbids, and a default would make that silent.

**Downgrade refuses** while any workspace has a zone other than the backfill's
`Africa/Cairo`: that choice would be lost. Otherwise it drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
BACKFILL = "Africa/Cairo"
BATCH = 1000
FIRST = "00000000-0000-0000-0000-000000000000"
NOT_NULL_CHECK = "ck_tenants_timezone_present"


def _backfill() -> int:
    bind = op.get_bind()
    after = FIRST
    changed = 0
    while True:
        ids = list(
            bind.execute(
                sa.text("SELECT id FROM tenants WHERE id > :after ORDER BY id LIMIT :limit"),
                {"after": after, "limit": BATCH},
            ).scalars()
        )
        if not ids:
            return changed
        result = bind.execute(
            sa.text(
                "UPDATE tenants SET timezone = :zone WHERE id = ANY(:ids) AND timezone IS NULL"
            ),
            {"zone": BACKFILL, "ids": ids},
        )
        changed += result.rowcount or 0
        after = str(ids[-1])


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("tenants", sa.Column("timezone", sa.String(64), nullable=True))
    _backfill()
    op.execute(
        f"ALTER TABLE tenants ADD CONSTRAINT {NOT_NULL_CHECK} CHECK (timezone IS NOT NULL) NOT VALID"
    )
    op.execute(f"ALTER TABLE tenants VALIDATE CONSTRAINT {NOT_NULL_CHECK}")
    op.execute("ALTER TABLE tenants ALTER COLUMN timezone SET NOT NULL")
    op.execute(f"ALTER TABLE tenants DROP CONSTRAINT {NOT_NULL_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    chosen = bind.execute(
        sa.text("SELECT count(*) FROM tenants WHERE timezone <> :zone"), {"zone": BACKFILL}
    ).scalar_one()
    if chosen:
        raise RuntimeError(
            "Workspace time zones cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaign page features (0108-0119)'). "
            f"Nothing has been changed:\n  workspaces with a zone other than {BACKFILL}: {chosen}"
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("tenants", "timezone")
