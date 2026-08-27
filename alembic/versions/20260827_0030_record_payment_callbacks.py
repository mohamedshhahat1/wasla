"""Record provider payment callbacks exactly once, and the intent behind one

ADR-044. Two changes, both small, and the first is the load-bearing one.

`payment_events` exists for its unique constraint. Every payment processor
retries a callback it did not get a 2xx for, and processing a retry a second
time settles an invoice twice and extends a billing period twice. The insert is
the claim: whoever writes the row owns the event, and whoever hits the
constraint knows somebody else already does. An application-level check cannot
give that, because two retries arriving together both read no row.

`payments.provider_intent_reference` is nullable and has no backfill, which is
correct rather than lazy: it holds the provider's id for an *intended* payment,
and no payment that predates hosted checkout ever had one. Writing anything
into those rows would be inventing a reference that resolves to nothing in the
provider's dashboard.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("provider_intent_reference", sa.String(200), nullable=True),
    )

    op.create_table(
        "payment_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_event_id", sa.String(200), nullable=False),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=True),
            # CASCADE: a record of a callback about a deleted payment is a
            # record about nothing. Nullable, because an authenticated callback
            # naming a payment we do not have is still worth recording.
            sa.ForeignKey("payments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(50), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_payment_events_provider_provider_event_id",
        ),
    )
    op.create_index("ix_payment_events_payment_id", "payment_events", ["payment_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_events_payment_id", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_column("payments", "provider_intent_reference")
