"""record when a workspace's claim on a phone number began

Revision ID: 0054
Revises: 0053

Inbound traffic was attributed to whoever holds a number *now*. Meta retries an
undelivered webhook for up to seven days, so a message a customer sent to
workspace A before A released the number could arrive after workspace B had
claimed it - and be projected into B's inbox, body and customer phone number
and all (MSG-01). The same resolution dropped A's in-flight delivery statuses
on the floor (MSG-04).

Answering "who held this number when this event happened" needs a tenure
interval, and only half of one existed: ``released_at`` says when a claim
ended, and nothing said when it began. ``created_at`` happens to coincide today
- ``WhatsAppAccountRepository.connect`` is the only writer of this table and it
sets both in the same statement - but that is a fact about one call site, not
an invariant, and routing customer messages between workspaces is not a
decision that should rest on a generic audit column continuing to mean
something specific.

So the interval gets its own column. ``ownership_started_at`` is backfilled
from ``created_at``, which is accurate for every row this schema can contain,
and from here on it is set explicitly at claim time.

The index is on ``(phone_number_id, ownership_started_at)`` rather than on
``phone_number_id`` alone: the historical lookup asks for the claims on one
number ordered newest first, and stops at the first interval that contains the
event. Partial on nothing - released rows are exactly the rows this query
exists to find, so the live-only index beside it cannot serve it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_accounts",
        sa.Column("ownership_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Every existing row was created by `connect`, which claims the number in
    # the same statement that inserts the row. Backfilled rather than defaulted
    # to now(): a claim made last year did not start today, and an event from
    # last month must still resolve to it.
    op.execute("UPDATE whatsapp_accounts SET ownership_started_at = created_at")
    op.alter_column(
        "whatsapp_accounts",
        "ownership_started_at",
        nullable=False,
        server_default=sa.func.now(),
    )
    op.create_index(
        "ix_whatsapp_accounts_phone_number_id_ownership_started_at",
        "whatsapp_accounts",
        ["phone_number_id", "ownership_started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_accounts_phone_number_id_ownership_started_at",
        table_name="whatsapp_accounts",
    )
    op.drop_column("whatsapp_accounts", "ownership_started_at")
