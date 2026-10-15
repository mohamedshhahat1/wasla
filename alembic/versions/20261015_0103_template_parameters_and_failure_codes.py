"""A campaign's header and button parameters, and the provider's code on a failed message.

Revision ID: 0103
Revises: 0102

CMP-05 / CMP-06 / CMP-10 (ADR-137, ADR-139).

1. `campaigns.template_parameters` (JSONB) - the header's and buttons'
   send-time parameters a template's stored components need. A campaign sent
   the body alone, so Meta refused every recipient of a template with a
   header or button parameter.
2. `messages.failure_code` (integer) - Meta's own numeric code for why a
   message failed, refused at send or reported by a later `failed` status.
   What a campaign-wide refusal (132000), a per-person limit (131049), an
   opt-out (131050) or a template pause (132015) is recognised by; the fixed
   sentence in `failure_reason` stays for people.

**Online.** Both are nullable `ADD COLUMN`s without a default: metadata-only,
including on `messages`, under a bounded `lock_timeout` so a busy table is
never queued behind.

**Downgrade refuses** while any campaign carries template parameters or any
message carries a code: dropping either loses what was sent or why it failed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaigns", sa.Column("template_parameters", JSONB(), nullable=True))
    op.add_column("messages", sa.Column("failure_code", sa.Integer(), nullable=True))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    parameters = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE template_parameters IS NOT NULL"
    ).scalar_one()
    if parameters:
        found.append(f"campaigns carrying header or button parameters: {parameters}")
    codes = bind.exec_driver_sql(
        "SELECT count(*) FROM messages WHERE failure_code IS NOT NULL"
    ).scalar_one()
    if codes:
        found.append(f"messages carrying a provider failure code: {codes}")
    if found:
        raise RuntimeError(
            "Template parameters and failure codes cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaigns remediation (0101-0106)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("messages", "failure_code")
    op.drop_column("campaigns", "template_parameters")
