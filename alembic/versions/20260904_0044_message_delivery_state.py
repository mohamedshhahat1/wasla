"""Give an outbound send a state, so a message Meta may hold is not sent twice.

Revision ID: 0044
Revises: 0043

`messages.status` described an outbound send completely while the row and the
Meta request became durable in the same transaction. ADR-093 splits them - the
send intent commits before Meta is asked to deliver anything - which introduces
a distinction `status` has no way to carry:

    pending, nothing asked of Meta   safe. Nobody has received anything.
    pending, the request was made    a customer may already be reading it.

Those are the same value in the old encoding, and telling them apart is the
whole of the fix: the first may be sent, the second may never be sent again on
its own account. So the state becomes a column.

## What this adds

- `delivery_state`, a native enum, NULL for every inbound message and for every
  row written before this migration.
- `ix_messages_unresolved_delivery`, the query an operator runs to find sends
  whose outcome nobody knows. Partial, and on a healthy deployment empty.

There is no unique index here, and the difference from `0042` is worth stating.
An invoice may be charged at most once, so a partial unique index on
`invoice_id` is the guarantee. A conversation may be written to as often as a
business likes, so there is no column a uniqueness constraint could be built on
- a second message to the same customer is the product working. What stops a
repeat is that no caller may act on a `requested` row, which is a rule about
reading rather than about writing.

## The backfill

Every existing row is classified by two columns it already carries.

`direction = 'inbound'` keeps NULL. Nothing here describes a message somebody
sent *to* the business.

An outbound row with a terminal status - `sent`, `delivered`, `read` - becomes
`sent`: Meta named it, which is what a `wa_message_id` on the row means.
`failed` becomes `undelivered`, which is what the old code wrote only after Meta
had read the request and declined it.

An outbound row still `pending` becomes **`requested`**, the conservative
reading and deliberately so. Under the old protocol the row committed alongside
the provider call, so a surviving `pending` is a send whose request may well
have been made and whose answer was lost. Calling it `requested` costs somebody
a look at the conversation; calling it `claimed` would tell the next caller it
was safe to send again. The migration takes the answer whose worst case is a
person reading a thread rather than a customer reading the same message twice.

On a healthy deployment this class is empty for a different reason than in
`0042`: nothing sweeps outbound messages, so a `pending` row is one whose send
never completed, and those are rare rather than impossible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

STATES = ("claimed", "requested", "sent", "undelivered")

# Restated here rather than imported from the model: a migration has to keep
# working when the model moves on.
UNRESOLVED = "delivery_state IN ('claimed', 'requested')"


def upgrade() -> None:
    state = sa.Enum(*STATES, name="message_delivery_state")
    state.create(op.get_bind(), checkfirst=True)

    op.add_column("messages", sa.Column("delivery_state", state, nullable=True))

    op.execute("""
        UPDATE messages
        SET delivery_state = CASE
            WHEN status = 'pending' THEN 'requested'
            WHEN status = 'failed' THEN 'undelivered'
            ELSE 'sent'
        END::message_delivery_state
        WHERE direction = 'outbound'
        """)

    op.create_index(
        "ix_messages_unresolved_delivery",
        "messages",
        ["tenant_id", "created_at"],
        unique=False,
        postgresql_where=sa.text(UNRESOLVED),
    )


def downgrade() -> None:
    """Reversible, and it loses exactly one thing: which sends were in flight.

    A row in `requested` describes a message Meta may hold and whose answer
    never arrived. The previous release has no column for that and reads it as
    an ordinary `pending` message, which is what it was before this migration
    existed. Nothing is deleted and no status is rewritten: `status` was never
    touched on the way up, so there is nothing to undo on the way down.
    """
    op.drop_index("ix_messages_unresolved_delivery", table_name="messages")
    op.drop_column("messages", "delivery_state")
    sa.Enum(name="message_delivery_state").drop(op.get_bind(), checkfirst=True)
