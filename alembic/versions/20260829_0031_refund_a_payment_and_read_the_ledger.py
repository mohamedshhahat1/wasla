"""Refunds, request idempotency, and a provider-event ledger worth reading

ADR-045. Three groups of changes, and each one exists because of a specific
thing that could not otherwise be done or said.

**Refunds.** `payments` gains the columns that describe money going back:
how much (`refunded_amount`), when it was asked for and when it was confirmed
(`refund_requested_at`, `refunded_at`), and the provider's id for the reversal
transaction, which is a *different* transaction from the one being reversed
(`refund_reference`). The two timestamps are deliberately separate: a refund
requested and never confirmed is the state that means the callback URL is
wrong and a customer is waiting, and one column cannot express it.

**Request idempotency.** `payments.idempotency_key` with a unique constraint
per workspace, so a retried checkout request is recognised instead of opening
a second payment page. Scoped to the tenant because the key comes from that
workspace's own client; a global constraint would let one customer deny
another a checkout by guessing a string.

**The ledger.** `payment_events` was recording every callback with an outcome
of "applied" regardless of what actually happened - an event naming an unknown
payment, or reporting an amount that disagreed with the invoice, was refused
and then filed as a success. `outcome` is now written after the decision, and
the columns added here are what make the row answer a question afterwards:
what the provider reported (`event_type`), which transaction it was about
(`provider_transaction_id`), when it arrived as distinct from when it was
decided (`received_at`), and why the decision went the way it did (`detail`).

`processed_at` becomes nullable as part of that. The row is inserted *before*
the decision - the insert is the claim, which is what makes two simultaneous
deliveries safe - so between the claim and the decision there is genuinely no
processing time, and a crash in that window should leave a row that says so.

Existing rows are backfilled from what is already known rather than guessed
at: `received_at` from the processing time that was recorded, `event_type`
from the literal that every row of this table had until now.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def _add_enum_values(type_name: str, values: tuple[str, ...]) -> None:
    """Extend a native enum, outside the migration's transaction.

    `ADD VALUE` cannot be used later in the same transaction that added it, and
    Alembic runs a migration inside one. `autocommit_block` is the supported
    way round that. `IF NOT EXISTS` makes the step re-runnable after a partial
    failure, which matters precisely because this part is not covered by the
    surrounding transaction.
    """
    with op.get_context().autocommit_block():
        for value in values:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")


def upgrade() -> None:
    _add_enum_values(
        "audit_action",
        (
            "payment_refund_requested",
            "payment_refunded",
            "subscription_past_due",
        ),
    )

    # ------------------------------------------------------------- payments
    op.add_column(
        "payments",
        sa.Column(
            "refunded_amount",
            sa.Numeric(12, 2),
            nullable=False,
            # A server default only for the length of this migration: adding a
            # NOT NULL column to a table with rows in it needs one, and zero is
            # a fact rather than a placeholder because nothing has been
            # refunded yet.
            server_default=sa.text("0.00"),
        ),
    )
    # Dropped again immediately, because the model does not declare one and a
    # default that exists in the database and not in the mapping is schema
    # drift - `alembic check` fails on it, which is exactly what that gate is
    # for. New rows get their zero from SQLAlchemy's own column default.
    op.alter_column("payments", "refunded_amount", server_default=None)
    op.add_column(
        "payments",
        sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("refund_reference", sa.String(200), nullable=True))
    op.add_column("payments", sa.Column("idempotency_key", sa.String(100), nullable=True))
    op.create_unique_constraint(
        "uq_payments_tenant_id_idempotency_key",
        "payments",
        ["tenant_id", "idempotency_key"],
    )

    # ------------------------------------------------------- payment_events
    op.add_column(
        "payment_events",
        sa.Column("provider_transaction_id", sa.String(200), nullable=True),
    )
    op.add_column(
        "payment_events",
        sa.Column("event_type", sa.String(100), nullable=True),
    )
    op.add_column("payment_events", sa.Column("detail", sa.String(300), nullable=True))
    op.add_column(
        "payment_events",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfilled from what the rows already say. Every event this table has
    # ever held was a transaction callback that was believed, and the moment it
    # was processed is the closest honest answer to when it arrived.
    op.execute("UPDATE payment_events SET received_at = processed_at WHERE received_at IS NULL")
    op.execute(
        "UPDATE payment_events SET event_type = 'transaction.succeeded' WHERE event_type IS NULL"
    )
    op.execute(
        "UPDATE payment_events SET provider_transaction_id = provider_event_id "
        "WHERE provider_transaction_id IS NULL"
    )

    op.alter_column("payment_events", "received_at", nullable=False)
    op.alter_column("payment_events", "event_type", nullable=False)
    # Nullable from here: the row is claimed before it is decided.
    op.alter_column("payment_events", "processed_at", nullable=True)

    op.create_index(
        "ix_payment_events_provider_received_at",
        "payment_events",
        ["provider", "received_at"],
    )
    op.create_index("ix_payment_events_outcome", "payment_events", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_payment_events_outcome", table_name="payment_events")
    op.drop_index("ix_payment_events_provider_received_at", table_name="payment_events")

    # A claimed-but-undecided row has no processing time, and the column is
    # about to stop allowing that. Given a value rather than deleted: it is
    # still the record that a callback arrived, which is the thing an operator
    # would be looking for.
    op.execute("UPDATE payment_events SET processed_at = received_at WHERE processed_at IS NULL")
    op.alter_column("payment_events", "processed_at", nullable=False)
    op.drop_column("payment_events", "received_at")
    op.drop_column("payment_events", "detail")
    op.drop_column("payment_events", "event_type")
    op.drop_column("payment_events", "provider_transaction_id")

    op.drop_constraint("uq_payments_tenant_id_idempotency_key", "payments", type_="unique")
    op.drop_column("payments", "idempotency_key")
    op.drop_column("payments", "refund_reference")
    op.drop_column("payments", "refunded_at")
    op.drop_column("payments", "refund_requested_at")
    op.drop_column("payments", "refunded_amount")

    # The enum values stay. PostgreSQL cannot remove one, and recreating
    # `audit_action` would mean rewriting every audit row to drop three labels
    # nothing points at - which is a great deal of risk to tidy up a type. The
    # same decision migration 0023 made, for the same reason.
