"""A connection's own sending allowance.

Revision ID: 0090
Revises: 0089

OMNI-052 (ADR-123, amended). One deployment-wide `CONNECTION_SENDS_PER_MINUTE`
fitted every connection, while Meta gives a number 80 messages a second by
default, up to 1,000 after an upgrade, and fixes a Coexistence number at 20.
`channel_connections.sends_per_minute` overrides the deployment's value for one
connection; null keeps it.

**Online.** A nullable `ADD COLUMN` is metadata-only. Its CHECK is added
`NOT VALID` and then validated, so the validating scan holds only a
`SHARE UPDATE EXCLUSIVE` lock.

**Downgrade refuses** while any connection carries its own allowance.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
CONSTRAINT = "ck_channel_connections_sends_per_minute_positive"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("channel_connections", sa.Column("sends_per_minute", sa.Integer(), nullable=True))
    op.execute(
        f"ALTER TABLE channel_connections ADD CONSTRAINT {CONSTRAINT}"
        " CHECK (sends_per_minute IS NULL OR sends_per_minute > 0) NOT VALID"
    )
    op.execute(f"ALTER TABLE channel_connections VALIDATE CONSTRAINT {CONSTRAINT}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    count = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM channel_connections WHERE sends_per_minute IS NOT NULL"
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Connections carry their own sending allowance "
            "(docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0091)'). "
            f"Nothing has been changed: {count} connections"
        )
    op.execute(f"ALTER TABLE channel_connections DROP CONSTRAINT {CONSTRAINT}")
    op.drop_column("channel_connections", "sends_per_minute")
    op.execute("RESET lock_timeout")
