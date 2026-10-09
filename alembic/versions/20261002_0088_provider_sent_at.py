"""The provider's own time for each sent message.

Revision ID: 0088
Revises: 0087

OMNI-042. A Messenger read watermark is "all messages sent before or at this
timestamp", on Meta's clock; `messages.sent_at` is Wasla's, taken after the Send
API answered, so the newest message sat a round trip after the watermark and was
never marked read. **`messages.provider_sent_at`** holds the provider's own time
- from a send receipt, an echo or a `sent` status - and the watermark is compared
with it.

**Online.** One nullable column with no default on `messages`: a metadata-only
`ADD COLUMN` under a bounded `lock_timeout`, no rewrite of the largest table.

**Downgrade refuses** while any message carries a provider time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "messages", sa.Column("provider_sent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    count = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM messages WHERE provider_sent_at IS NOT NULL")
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Messages carry the provider's own send time "
            "(docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0091)'). "
            f"Nothing has been changed: {count} messages"
        )
    op.drop_column("messages", "provider_sent_at")
    op.execute("RESET lock_timeout")
