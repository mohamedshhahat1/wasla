"""record what produced each message, rather than inferring it

Revision ID: 0056
Revises: 0055

Attribution was inferred from `sent_by_id`, and the inference was wrong twice.
A campaign carries its creator, so every broadcast read as that person's
reply; a follow-up carries nobody, so every nudge read as an AI reply
(MSG-16). Both were recoverable by joining `campaign_recipients` or
`follow_ups` on `message_id` - but not by *reading the transcript*, which is
what an auditor, an analytics query and a colleague scrolling the inbox all
actually do.

**The backfill is exact, not a guess**, which is why it is worth doing rather
than leaving the column null for history. Every existing outbound message has
a determinate origin recoverable from the same joins the inference should have
used, and the order below is the order of certainty:

    campaign_recipients.message_id  -> campaign    (a campaign names its sends)
    follow_ups.message_id           -> follow_up   (so does a follow-up)
    sent_by_id IS NOT NULL          -> human       (a user pressed send)
    otherwise, outbound             -> agent       (the only remaining producer)
    inbound                         -> customer

The two joins run first precisely because they are the two cases the naive
inference got wrong; anything they claim is claimed on evidence rather than on
the absence of it.

`agent` as the final outbound fallback is safe because the four producers are
the complete set - `MessagingService` is the only writer of outbound rows and
has exactly four callers - and the two that carry a `sent_by_id` have already
been taken by the clauses above.

The column is added nullable, backfilled, then made NOT NULL, so the table is
never rewritten with a default that would then have to be corrected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

ORIGINS = ("customer", "human", "agent", "campaign", "follow_up", "system")


def upgrade() -> None:
    origin = sa.Enum(*ORIGINS, name="message_origin", native_enum=True)
    origin.create(op.get_bind(), checkfirst=True)
    op.add_column("messages", sa.Column("origin", origin, nullable=True))

    # Inbound first and unconditionally: a customer's message has one possible
    # origin and no join can say otherwise.
    op.execute("UPDATE messages SET origin = 'customer' WHERE direction = 'inbound'")

    # Then the two the old inference got wrong, on the evidence of the rows
    # that name the message.
    op.execute("""
        UPDATE messages m SET origin = 'campaign'
        FROM campaign_recipients r
        WHERE r.message_id = m.id AND m.origin IS NULL
        """)
    op.execute("""
        UPDATE messages m SET origin = 'follow_up'
        FROM follow_ups f
        WHERE f.message_id = m.id AND m.origin IS NULL
        """)

    # What is left is a human reply or an agent reply, and `sent_by_id`
    # distinguishes those correctly - it was only the two cases above that made
    # it unreliable.
    op.execute("UPDATE messages SET origin = 'human' WHERE origin IS NULL AND sent_by_id IS NOT NULL")
    op.execute("UPDATE messages SET origin = 'agent' WHERE origin IS NULL")

    op.alter_column("messages", "origin", nullable=False)


def downgrade() -> None:
    op.drop_column("messages", "origin")
    sa.Enum(name="message_origin").drop(op.get_bind(), checkfirst=True)
