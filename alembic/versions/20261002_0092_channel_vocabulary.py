"""Telegram and TikTok join the channel vocabulary.

Revision ID: 0092
Revises: 0091

ENT-21 (ADR-131). A plan names the channel types a workspace may connect, and a
channel top-up may be typed for one of them, so every channel the product owner
has named must exist as a label - including two with no adapter at all. A label
is vocabulary, not support (ADR-117): `ChannelRegistry` still operates WhatsApp
only, and nothing can connect or send on Telegram or TikTok.

`ALTER TYPE ... ADD VALUE` runs in an autocommit block of its own, first, as
0082's and 0087's did: it cannot run inside a transaction block, and the later
entitlement migrations use the labels.

**Downgrade** cannot drop an enum label - PostgreSQL has no `DROP VALUE` - so it
leaves the labels in place, where they are harmless and a re-upgrade finds them
(`IF NOT EXISTS`). It refuses while any row names one, because a schema that
does not know the label is one where those rows cannot be explained.
"""

from __future__ import annotations

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

LABELS: tuple[str, ...] = ("telegram", "tiktok")

# Every column typed `channel_kind`, so a downgrade can say which rows it
# would strand.
CHANNEL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("channel_connections", "channel"),
    ("contact_identities", "channel"),
    ("conversations", "channel"),
    ("whatsapp_events", "channel"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE channel_kind ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    quoted = ", ".join(f"'{label}'" for label in LABELS)
    for table, column in CHANNEL_COLUMNS:
        count = bind.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {column}::text IN ({quoted})"  # noqa: S608 - module constants
        ).scalar_one()
        if count:
            found.append(f"{table}: {count}")
    if found:
        raise RuntimeError(
            "Rows name the telegram or tiktok channel "
            "(docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0097)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
