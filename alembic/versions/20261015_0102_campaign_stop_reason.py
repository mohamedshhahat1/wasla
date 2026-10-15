"""Why a campaign is paused or failed, and the campaign actions the audit log lacked.

Revision ID: 0102
Revises: 0101

CMP-02 / CMQ-11 / CMP-14 / CMP-23 (ADR-135).

1. **Labels**, appended in an autocommit block first, as every label is:
   `audit_action` gains `campaign_created`, `campaign_audience_set` and
   `campaign_paused`. Enum labels cannot be dropped; the downgrade leaves them.
2. `campaigns.status_reason` - a closed vocabulary (`CampaignStopReason`) for a
   paused or failed campaign: a suspended or deleted workspace, a suspended
   subscription, an admin's pause, a template or credential refusal.

**Online.** A nullable `ADD COLUMN` without a default is metadata-only. Its
CHECK is added `NOT VALID` and then validated, so the validating scan holds
only a `SHARE UPDATE EXCLUSIVE` lock; every existing row is null.

**Downgrade refuses** while any campaign carries a reason or any audit entry
records one of the new actions: dropping either loses why a campaign stopped
or who acted on it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
CONSTRAINT = "ck_campaigns_status_reason_known"
LABELS = ("campaign_created", "campaign_audience_set", "campaign_paused")
REASONS = (
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
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaigns", sa.Column("status_reason", sa.String(40), nullable=True))
    allowed = ", ".join(f"'{reason}'" for reason in REASONS)
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {CONSTRAINT}"
        f" CHECK (status_reason IS NULL OR status_reason IN ({allowed})) NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {CONSTRAINT}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    reasons = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE status_reason IS NOT NULL"
    ).scalar_one()
    if reasons:
        found.append(f"campaigns carrying a status_reason: {reasons}")
    labels = ", ".join(f"'{label}'" for label in LABELS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({labels})"  # noqa: S608 - module constants
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a campaign creation, audience or pause: {audited}")
    if found:
        raise RuntimeError(
            "Campaign stop reasons cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaigns remediation (0101-0106)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.drop_column("campaigns", "status_reason")
