"""What customers did after a campaign: replied, opted out.

Revision ID: 0118
Revises: 0117

PG-10 (ADR-149). `campaign_recipients.replied_at` and `opted_out_after_at`:
recorded once, at ingestion and at the opt-out, on the contact's latest
accepted copy inside the attribution window. Their lookup,
`ix_campaign_recipients_tenant_id_contact_id_sent_at` (partial: sent rows),
is built `CONCURRENTLY`.

**Online.** Two nullable columns without defaults (catalogue-only) under a
bounded `lock_timeout`; the index concurrently (a leftover INVALID build of a
failed run dropped first, 0100's pattern).

**Downgrade refuses** while any recipient records a reply or an opt-out after.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
INDEX = "ix_campaign_recipients_tenant_id_contact_id_sent_at"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "campaign_recipients", sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "campaign_recipients",
        sa.Column("opted_out_after_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("RESET lock_timeout")
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constant
                f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
            )
            .scalar_one()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON campaign_recipients"
            " (tenant_id, contact_id, sent_at DESC) WHERE sent_at IS NOT NULL"
        )


def downgrade() -> None:
    recorded = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM campaign_recipients"
            " WHERE replied_at IS NOT NULL OR opted_out_after_at IS NOT NULL"
        )
        .scalar_one()
    )
    if recorded:
        raise RuntimeError(
            f"Campaign results cannot be downgraded ({RUNBOOK}). "
            f"Nothing has been changed:\n  recipients with a reply or an opt-out after: {recorded}"
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.drop_column("campaign_recipients", "opted_out_after_at")
    op.drop_column("campaign_recipients", "replied_at")
