"""Quiet hours, and a live campaign that waits by itself.

Revision ID: 0109
Revises: 0108

TZ-03 / TZ-04 (ADR-145); the waiting state LIM-04 and FREQ-01 use too.

1. **Labels**, appended in an autocommit block first, as every label is:
   `audit_action` gains `campaign_settings_updated` and `campaign_waiting`.
   Enum labels cannot be dropped; the downgrade leaves them.
2. `tenants.campaign_quiet_hours_enabled` (true), `campaign_quiet_hours_start`
   (22:00) and `campaign_quiet_hours_end` (09:00), every workspace's quiet
   hours in its own zone - on from the start, for new and existing workspaces
   alike. `ck_tenants_campaign_quiet_hours_window` refuses an empty window,
   `ck_tenants_campaign_quiet_hours_minutes` a time with seconds.
3. `campaigns.waiting_reason` (text, `ck_campaigns_waiting_reason_known`:
   quiet_hours, daily_limit, contact_cap, limit_unknown) and
   `campaigns.resumes_at`: why a live campaign is sending nothing, and when
   it will by itself.

**Online.** The three `tenants` columns carry constant defaults, which
PostgreSQL stores in the catalogue rather than writing to every row; the two
`campaigns` columns are nullable without a default. Every CHECK is added
`NOT VALID` and validated under a bounded `lock_timeout`, holding only
`SHARE UPDATE EXCLUSIVE` while it scans. No table rewrite.

**Downgrade refuses** while any workspace's quiet hours differ from the
defaults, any campaign is waiting or records when it resumes, or any audit
entry records a settings change or a wait: each would be lost. Otherwise it
drops exactly what the upgrade added, in the run's transaction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABELS = ("campaign_settings_updated", "campaign_waiting")
WAIT_REASONS = ("quiet_hours", "daily_limit", "contact_cap", "limit_unknown")
WINDOW_CHECK = "ck_tenants_campaign_quiet_hours_window"
MINUTES_CHECK = "ck_tenants_campaign_quiet_hours_minutes"
WAIT_CHECK = "ck_campaigns_waiting_reason_known"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0120)'"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "tenants",
        sa.Column(
            "campaign_quiet_hours_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "campaign_quiet_hours_start",
            sa.Time(),
            nullable=False,
            server_default=sa.text("'22:00'"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "campaign_quiet_hours_end",
            sa.Time(),
            nullable=False,
            server_default=sa.text("'09:00'"),
        ),
    )
    op.add_column("campaigns", sa.Column("waiting_reason", sa.String(40), nullable=True))
    op.add_column("campaigns", sa.Column("resumes_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        f"ALTER TABLE tenants ADD CONSTRAINT {WINDOW_CHECK}"
        " CHECK (campaign_quiet_hours_start <> campaign_quiet_hours_end) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE tenants ADD CONSTRAINT {MINUTES_CHECK}"
        " CHECK (date_part('second', campaign_quiet_hours_start) = 0"
        " AND date_part('second', campaign_quiet_hours_end) = 0) NOT VALID"
    )
    allowed = ", ".join(f"'{reason}'" for reason in WAIT_REASONS)
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {WAIT_CHECK}"
        f" CHECK (waiting_reason IS NULL OR waiting_reason IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE tenants VALIDATE CONSTRAINT {WINDOW_CHECK}")
    op.execute(f"ALTER TABLE tenants VALIDATE CONSTRAINT {MINUTES_CHECK}")
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {WAIT_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    changed = bind.exec_driver_sql(
        "SELECT count(*) FROM tenants WHERE NOT campaign_quiet_hours_enabled"
        " OR campaign_quiet_hours_start <> '22:00' OR campaign_quiet_hours_end <> '09:00'"
    ).scalar_one()
    if changed:
        found.append(f"workspaces whose quiet hours differ from 22:00-09:00, on: {changed}")
    waiting = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE waiting_reason IS NOT NULL OR resumes_at IS NOT NULL"
    ).scalar_one()
    if waiting:
        found.append(f"campaigns waiting, or recording when they resume: {waiting}")
    labels = ", ".join(f"'{label}'" for label in LABELS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({labels})"  # noqa: S608 - module constants
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a settings change or a wait: {audited}")
    if found:
        raise RuntimeError(
            f"Quiet hours cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {WAIT_CHECK}")
    op.execute(f"ALTER TABLE tenants DROP CONSTRAINT IF EXISTS {MINUTES_CHECK}")
    op.execute(f"ALTER TABLE tenants DROP CONSTRAINT IF EXISTS {WINDOW_CHECK}")
    op.drop_column("campaigns", "resumes_at")
    op.drop_column("campaigns", "waiting_reason")
    op.drop_column("tenants", "campaign_quiet_hours_end")
    op.drop_column("tenants", "campaign_quiet_hours_start")
    op.drop_column("tenants", "campaign_quiet_hours_enabled")
