"""A campaign's deadline.

Revision ID: 0112
Revises: 0111

LIM-06 (ADR-146). `campaigns.send_until`: when a campaign stops trying.
Whoever is still pending then is skipped with the new reason code
`send_window_closed`, and the campaign completes - a spread campaign about an
offer that ends on Friday stops on Friday.

1. `campaigns.send_until`, nullable without a default: catalogue-only.
2. `ck_campaigns_send_until_after_start`: a deadline comes after the start.
3. `ck_campaign_recipients_reason_code_known` gains `send_window_closed`.

**Online.** Both CHECKs are replaced `NOT VALID` and validated under a bounded
`lock_timeout`, holding only `SHARE UPDATE EXCLUSIVE` while they scan. No
rewrite of `campaigns` or `campaign_recipients`.

**Downgrade refuses** while any campaign has a deadline or any recipient was
skipped by one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
REASON_CHECK = "ck_campaign_recipients_reason_code_known"
DEADLINE_CHECK = "ck_campaigns_send_until_after_start"
REASONS_0104 = (
    "opted_out",
    "contact_gone",
    "other_connection",
    "identity_changed",
    "channel_not_in_plan",
    "recipient_opted_out_at_provider",
    "marketing_limit_per_user",
    "undeliverable",
    "delivery_unconfirmed",
    "attempts_exhausted",
    "template_parameters_refused",
)
NEW_REASONS = ("send_window_closed",)
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0120)'"


def _reason_check(reasons: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{reason}'" for reason in reasons)
    return f"CHECK (reason_code IS NULL OR reason_code IN ({allowed}))"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaigns", sa.Column("send_until", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {DEADLINE_CHECK} CHECK (send_until IS NULL"
        " OR scheduled_at IS NULL OR send_until > scheduled_at) NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK}"
        f" {_reason_check(REASONS_0104 + NEW_REASONS)} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {DEADLINE_CHECK}")
    op.execute(f"ALTER TABLE campaign_recipients VALIDATE CONSTRAINT {REASON_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    deadlines = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE send_until IS NOT NULL"
    ).scalar_one()
    if deadlines:
        found.append(f"campaigns with a deadline: {deadlines}")
    skipped = bind.exec_driver_sql(
        "SELECT count(*) FROM campaign_recipients WHERE reason_code = 'send_window_closed'"
    ).scalar_one()
    if skipped:
        found.append(f"recipients skipped by a deadline: {skipped}")
    if found:
        raise RuntimeError(
            f"Campaign deadlines cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK} "
        f"{_reason_check(REASONS_0104)}"
    )
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {DEADLINE_CHECK}")
    op.drop_column("campaigns", "send_until")
