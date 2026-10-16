"""Each person's own values, written down with them.

Revision ID: 0114
Revises: 0113

PER-02 (ADR-148). `campaign_recipients.rendered_parameters` (JSONB): the body
and header values resolved for this person when the audience was written,
with the contact they were resolved for and which of them fell back. The send
uses exactly these; editing the contact afterwards changes nothing this
campaign sends.

**Online.** A nullable column without a default: catalogue-only, no rewrite
of `campaign_recipients`, under a bounded `lock_timeout`.

**Downgrade refuses** while any recipient carries values: the campaign would
fall back to sending everybody the same constants.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0119)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaign_recipients", sa.Column("rendered_parameters", JSONB(), nullable=True))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    carrying = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM campaign_recipients WHERE rendered_parameters IS NOT NULL"
        )
        .scalar_one()
    )
    if carrying:
        raise RuntimeError(
            f"Per-person values cannot be downgraded ({RUNBOOK}). "
            f"Nothing has been changed:\n  recipients carrying their own values: {carrying}"
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("campaign_recipients", "rendered_parameters")
