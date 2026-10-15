"""The token of the claim that owns a campaign's sending.

Revision ID: 0101
Revises: 0100

CMP-01 (ADR-134). Two campaign workers sent the same people the same campaign:
the claim's row lock ends at the first send commit and nothing told a worker
its lease had been taken over. `campaigns.lease_token` names the claim that
owns the campaign; a worker proceeds only while the row still carries its own
token. Null when no claim holds it.

**Online.** A nullable `ADD COLUMN` without a default is metadata-only.

**Downgrade** drops the column. A token is a lease, not data: dropping it
releases every claim, which the next sweep re-takes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("campaigns", sa.Column("lease_token", UUID(as_uuid=True), nullable=True))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("campaigns", "lease_token")
    op.execute("RESET lock_timeout")
