"""One marketing message per customer per day, and Meta's portfolio pacing.

Revision ID: 0113
Revises: 0112

FREQ-01 (ADR-147; CMQ-03 decided) and LIM-08 (ADR-146).

1. `contacts.last_marketing_sent_at`: when this workspace last sent the person
   a marketing template - a campaign's or a person's. Claimed by one
   conditional `UPDATE` in the transaction that commits the send intent, so
   two campaigns on two workers can never both win the same 24 hours.
2. `messages.marketing_cap_claimed_at` and `messages.marketing_cap_previous_at`:
   which send made the claim and what it replaced, so a send Meta provably
   refused gives the claim back.
3. `messages.provider_held_at`: Meta accepted the message with
   `message_status: held_for_quality_assessment` - portfolio pacing holding it
   while it gathers feedback on the template ("Business portfolio pacing",
   developers.facebook.com, read 2026-10-16).
4. `ck_campaigns_status_reason_known` gains `meta_quality_review`: a held
   message dropped after review (status `failed`, code 135000) pauses the
   campaign, because Meta says the portfolio cannot send templates while it is
   under review.
5. `ck_campaign_recipients_reason_code_known` gains `contact_frequency_cap`.

**Online.** Nullable columns without defaults on `contacts` and `messages` -
catalogue-only, no rewrite of the two largest tables; both CHECKs replaced
`NOT VALID` and validated under a bounded `lock_timeout`.

**Downgrade refuses** while any contact carries a claim, any message records a
claim or a hold, any campaign stopped for review, or any recipient was skipped
by the cap.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
STOP_CHECK = "ck_campaigns_status_reason_known"
REASON_CHECK = "ck_campaign_recipients_reason_code_known"
STOP_REASONS_0107 = (
    "workspace_suspended",
    "workspace_deleted",
    "subscription_suspended",
    "admin_paused",
    "template_removed",
    "template_not_approved",
    "template_withdrawn",
    "template_category",
    "template_parameters_refused",
    "connection_disabled",
    "credential_refused",
    "connection_refused",
    "provider_unreachable",
    "repeated_batch_failure",
    "header_media_unavailable",
    "header_media_refused",
)
NEW_STOP_REASONS = ("meta_quality_review",)
REASONS_0112 = (
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
    "send_window_closed",
)
NEW_REASONS = ("contact_frequency_cap",)
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def _check(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"CHECK ({column} IS NULL OR {column} IN ({allowed}))"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "contacts", sa.Column("last_marketing_sent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "messages", sa.Column("marketing_cap_claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "messages",
        sa.Column("marketing_cap_previous_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "messages", sa.Column("provider_held_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT {STOP_CHECK}")
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {STOP_CHECK}"
        f" {_check('status_reason', STOP_REASONS_0107 + NEW_STOP_REASONS)} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK}"
        f" {_check('reason_code', REASONS_0112 + NEW_REASONS)} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {STOP_CHECK}")
    op.execute(f"ALTER TABLE campaign_recipients VALIDATE CONSTRAINT {REASON_CHECK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    claims = bind.exec_driver_sql(
        "SELECT count(*) FROM contacts WHERE last_marketing_sent_at IS NOT NULL"
    ).scalar_one()
    if claims:
        found.append(f"contacts carrying a marketing claim: {claims}")
    messages = bind.exec_driver_sql(
        "SELECT count(*) FROM messages WHERE marketing_cap_claimed_at IS NOT NULL"
        " OR provider_held_at IS NOT NULL"
    ).scalar_one()
    if messages:
        found.append(f"messages recording a marketing claim or a hold: {messages}")
    reviewed = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE status_reason = 'meta_quality_review'"
    ).scalar_one()
    if reviewed:
        found.append(f"campaigns stopped for Meta's quality review: {reviewed}")
    capped = bind.exec_driver_sql(
        "SELECT count(*) FROM campaign_recipients WHERE reason_code = 'contact_frequency_cap'"
    ).scalar_one()
    if capped:
        found.append(f"recipients skipped by the per-contact cap: {capped}")
    if found:
        raise RuntimeError(
            f"The per-contact cap and pacing cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK} "
        f"{_check('reason_code', REASONS_0112)}"
    )
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {STOP_CHECK}")
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {STOP_CHECK} "
        f"{_check('status_reason', STOP_REASONS_0107)}"
    )
    op.drop_column("messages", "provider_held_at")
    op.drop_column("messages", "marketing_cap_previous_at")
    op.drop_column("messages", "marketing_cap_claimed_at")
    op.drop_column("contacts", "last_marketing_sent_at")
