"""The `external` message origin: a reply the business sent from outside Wasla.

Revision ID: 0087
Revises: 0086

OMNI-037 (ADR-129). An echo of a reply a person typed in the provider's own app
(Instagram's and Messenger's `is_echo`; Coexistence's `smb_message_echoes`) is
projected as an outbound message, and needs an origin that is neither `human`
(no Wasla user sent it) nor `agent`.

`ALTER TYPE ... ADD VALUE` runs in an autocommit block of its own, first, as
0082's did: it cannot run inside a transaction block, and running it last would
commit it ahead of this revision's `alembic_version` row.

**Downgrade** cannot drop an enum label - PostgreSQL has no `DROP VALUE` - so it
leaves the label in place, where it is harmless and a re-upgrade finds it
(`IF NOT EXISTS`). It refuses while any message carries the origin, because a
schema that does not know the label is one where those rows cannot be explained.
"""

from __future__ import annotations

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_origin ADD VALUE IF NOT EXISTS 'external'")


def downgrade() -> None:
    count = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM messages WHERE origin::text = 'external'")
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Messages sent from outside Wasla carry the 'external' origin "
            "(docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0090)'). "
            f"Nothing has been changed: {count} messages"
        )
