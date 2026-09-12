"""give every message a durable position in its conversation

Revision ID: 0058
Revises: 0057

The conversation an agent reads was ordered by `messages.created_at`, and that
column is `now()` - PostgreSQL's transaction start. The ingestion loop writes
every message of one Meta delivery in one transaction, so five messages a
customer typed in a burst carried one instant between them, and the tie was
broken by a random UUID. The model was handed `FOURTH, FIFTH, SECOND, THIRD,
FIRST`, and with thirty messages against a twenty-message window the item it
read as "what the customer just said" was not the newest message (AI-01).

**The fix is a position, assigned by the database.** `messages.sequence` is
allocated at insert by a trigger doing one atomic `UPDATE conversations ...
RETURNING`, so concurrent writers to one conversation serialise on the
conversation row instead of racing a `max()+1`, and every producer - inbound
projection, agent, colleague, campaign, follow-up - takes part without having
to remember to.

**What the backfill can and cannot recover, stated plainly.** Existing rows are
numbered by `created_at`, then the Meta timestamp in `sent_at`, then `id`. That
is a deterministic total order, and it is the best one the stored data supports.
It is *not* a reconstruction of the true order for historical rows that share a
transaction timestamp and a Meta timestamp: that information was never
recorded, and no ordering of what is left can recover it. The goal of this
revision is that every new message is ordered correctly and every old one has a
stable, repeatable position.

The table is locked against inserts for the length of the backfill, so no row
can be written between numbering the history and installing the trigger.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

# Frozen copies. `app/db/models/conversation.py` holds the live definition; a
# migration must keep meaning what it meant on the day it shipped.
FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_assign_message_sequence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE conversations
       SET last_message_sequence = last_message_sequence + 1
     WHERE id = NEW.conversation_id
       AND tenant_id = NEW.tenant_id
    RETURNING last_message_sequence INTO NEW.sequence;
    NEW.sequence = COALESCE(NEW.sequence, 0);
    RETURN NEW;
END;
$$
"""

TRIGGER = """
CREATE TRIGGER trg_messages_assign_sequence
BEFORE INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION wasla_assign_message_sequence()
"""


def upgrade() -> None:
    # Held until this revision's transaction commits. SHARE ROW EXCLUSIVE
    # admits readers and refuses writers, which is exactly the window that
    # matters: a message inserted after numbering and before the trigger would
    # be left without a position.
    op.execute("LOCK TABLE messages IN SHARE ROW EXCLUSIVE MODE")

    op.add_column(
        "conversations",
        sa.Column(
            "last_message_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("messages", sa.Column("sequence", sa.BigInteger(), nullable=True))

    op.execute("""
        UPDATE messages m
           SET sequence = ordered.position
          FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY conversation_id
                           ORDER BY created_at, sent_at NULLS LAST, id
                       ) AS position
                  FROM messages
               ) ordered
         WHERE ordered.id = m.id
        """)
    op.execute("""
        UPDATE conversations c
           SET last_message_sequence = numbered.top
          FROM (
                SELECT conversation_id, max(sequence) AS top
                  FROM messages
                 GROUP BY conversation_id
               ) numbered
         WHERE numbered.conversation_id = c.id
        """)

    op.alter_column("messages", "sequence", nullable=False)
    op.create_unique_constraint(
        "uq_messages_conversation_id_sequence",
        "messages",
        ["conversation_id", "sequence"],
    )
    op.execute(FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_messages_assign_sequence ON messages")
    op.execute("DROP FUNCTION IF EXISTS wasla_assign_message_sequence()")
    op.drop_constraint("uq_messages_conversation_id_sequence", "messages", type_="unique")
    op.drop_column("messages", "sequence")
    op.drop_column("conversations", "last_message_sequence")
