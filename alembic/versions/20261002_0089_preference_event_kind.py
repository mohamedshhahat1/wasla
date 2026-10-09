"""The `preference` event kind: a marketing stop or resume made through the provider.

Revision ID: 0089
Revises: 0088

OMNI-046. WhatsApp's `user_preferences` webhook reports a person stopping or
resuming marketing messages in WhatsApp itself; it was refused as an unsupported
field. Its deliveries are now stored in the event log like every other event, so
a replay is recognised, and need a kind of their own.

`ALTER TYPE ... ADD VALUE` in its own autocommit block, first, as 0082 and 0087.
The label cannot be dropped; the downgrade refuses while any event carries it.
"""

from __future__ import annotations

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE whatsapp_event_kind ADD VALUE IF NOT EXISTS 'preference'")


def downgrade() -> None:
    count = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM whatsapp_events WHERE kind::text = 'preference'")
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Marketing preference events are stored under the 'preference' kind "
            "(docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0091)'). "
            f"Nothing has been changed: {count} events"
        )
