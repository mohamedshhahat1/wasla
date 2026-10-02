"""Channel-neutral message meters.

Revision ID: 0096
Revises: 0095

ADR-131 (ENT-22).

1. **`usage_event_type` gains** `message_received` and `message_sent` - the
   neutral message meters every channel but WhatsApp writes, appended in an
   autocommit block first, as every label is. WhatsApp keeps writing
   `whatsapp_message_received` / `whatsapp_message_sent`, which are these
   meters' WhatsApp instance mapped 1:1: no history is rewritten and no usage
   cycle is split between two labels.
2. **`ck_usage_events_neutral_message_meter_has_channel`** - a neutral message
   row names its channel (the dimension 0094 added). Added `NOT VALID` and
   validated in an autocommit block: the table may be large, and validation
   holds no lock that blocks writes. No row can violate it yet - the labels
   are new.

**Online.** Two enum labels and one CHECK; nothing is rewritten. `lock_timeout`
is bounded.

**Downgrade refuses** while any usage row uses either label: a message counted
under it would vanish from `period_messages`. Otherwise it drops the CHECK; the
labels stay (PostgreSQL cannot drop an enum label).
"""

from __future__ import annotations

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

LABELS = ("message_received", "message_sent")
CONSTRAINT = "ck_usage_events_neutral_message_meter_has_channel"
CHECK = "event_type NOT IN ('message_received', 'message_sent') OR channel IS NOT NULL"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE usage_event_type ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE usage_events ADD CONSTRAINT {CONSTRAINT} CHECK ({CHECK}) NOT VALID")
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE usage_events VALIDATE CONSTRAINT {CONSTRAINT}")


def downgrade() -> None:
    bind = op.get_bind()
    quoted = ", ".join(f"'{label}'" for label in LABELS)
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM usage_events WHERE event_type::text IN ({quoted})"  # noqa: S608
    ).scalar_one()
    if used:
        raise RuntimeError(
            "The neutral message meters cannot be downgraded "
            "(docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'). "
            f"Nothing has been changed:\n  usage_events on a neutral message meter: {used}"
        )
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
