"""Recipient reason codes and deferral; a campaign's outage and batch-failure state.

Revision ID: 0104
Revises: 0103

CMP-06 / CMP-07 / CMP-13 / CMP-18 / CMP-20 (ADR-139, ADR-141).

1. `campaign_recipients.reason_code` - why a recipient ended as it did, from a
   closed vocabulary (`RecipientReason`), beside the sentence in `last_error`.
2. `campaign_recipients.not_before` - Meta's pair rate limit (131056) defers
   this one person; the claim skips them until then.
3. `campaigns.provider_unreachable_since` - the start of a run of batches
   that could not reach Meta, which costs time, not attempts, until a bound.
4. `campaigns.consecutive_batch_failures` - batches in a row that raised; the
   campaign is paused at a bound instead of retried for ever.

**Online.** Every column is nullable without a default, except the counter,
whose constant `DEFAULT 0` is a catalogue-only change since PostgreSQL 11 -
no table rewrite. The two CHECKs are added `NOT VALID` and then validated,
holding only `SHARE UPDATE EXCLUSIVE` while they scan; every existing row
passes (null, or zero).

**Downgrade refuses** while any recipient carries a reason code or a
deferral, or any campaign an outage or failure count: each is state a
running campaign acts on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
REASON_CHECK = "ck_campaign_recipients_reason_code_known"
FAILURES_CHECK = "ck_campaigns_consecutive_batch_failures_non_negative"
REASONS = (
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


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaign_recipients", sa.Column("reason_code", sa.String(40), nullable=True))
    op.add_column(
        "campaign_recipients",
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("provider_unreachable_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "consecutive_batch_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    allowed = ", ".join(f"'{reason}'" for reason in REASONS)
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK}"
        f" CHECK (reason_code IS NULL OR reason_code IN ({allowed})) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {FAILURES_CHECK}"
        " CHECK (consecutive_batch_failures >= 0) NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_recipients VALIDATE CONSTRAINT {REASON_CHECK}")
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {FAILURES_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    recipients = bind.exec_driver_sql(
        "SELECT count(*) FROM campaign_recipients"
        " WHERE reason_code IS NOT NULL OR not_before IS NOT NULL"
    ).scalar_one()
    if recipients:
        found.append(f"campaign_recipients carrying a reason code or a deferral: {recipients}")
    campaigns = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns"
        " WHERE provider_unreachable_since IS NOT NULL OR consecutive_batch_failures <> 0"
    ).scalar_one()
    if campaigns:
        found.append(f"campaigns carrying an outage or batch-failure count: {campaigns}")
    if found:
        raise RuntimeError(
            "Campaign answer-class state cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaigns remediation (0101-0106)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {FAILURES_CHECK}")
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.drop_column("campaigns", "consecutive_batch_failures")
    op.drop_column("campaigns", "provider_unreachable_since")
    op.drop_column("campaign_recipients", "not_before")
    op.drop_column("campaign_recipients", "reason_code")
