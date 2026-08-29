"""Saved cards, automatic collection attempts, and the retry state behind them

ADR-046. Everything a renewal needs to be collected without a customer being
present, and nothing that would let card data near this database.

`payment_methods` holds what a processor gives back when a customer chooses to
save a card: an opaque token, the provider's id for it, and the last four
digits the provider already prints on receipts. **There is deliberately no
column for a card number, an expiry or a security code.** Those never arrive -
the customer types them into the provider's own page - and a schema with
nowhere to put them is a better guarantee than a rule saying not to.

Its unique constraint is the same mechanism the payment ledger uses: a
saved-card notification is retried like any other callback, so the insert has
to be the claim or a retry becomes a second card.

`payments` gains `is_automatic` and `payment_method_id`. Whether a person was
at a payment page is not a detail: it is a different event to the customer and
to the card scheme, and a dispute turns on which. The card reference is
`SET NULL` rather than `CASCADE`, because a payment outlives the card that made
it and the record of what was collected must not vanish when somebody removes a
card from their account.

`invoices` gains `collection_attempts` and `next_collection_at`. On the invoice
rather than the subscription because they count attempts at collecting *this*
bill - a customer who fixes their card next month starts from zero on next
month's invoice, which is what anybody would expect.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

PAYMENT_METHOD_STATUS = postgresql.ENUM(
    "active",
    "revoked",
    name="payment_method_status",
    create_type=False,
)


def upgrade() -> None:
    PAYMENT_METHOD_STATUS.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "payment_methods",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        # The processor's opaque handle. Not a card number.
        sa.Column("provider_token", sa.String(200), nullable=False),
        sa.Column("provider_token_id", sa.String(200), nullable=True),
        sa.Column("masked_pan", sa.String(40), nullable=True),
        sa.Column("brand", sa.String(40), nullable=True),
        sa.Column("status", PAYMENT_METHOD_STATUS, nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_payment_methods_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_methods"),
        sa.UniqueConstraint(
            "provider",
            "provider_token",
            name="uq_payment_methods_provider_provider_token",
        ),
    )
    op.create_index("ix_payment_methods_tenant_id", "payment_methods", ["tenant_id"])
    op.create_index(
        "ix_payment_methods_tenant_id_is_default",
        "payment_methods",
        ["tenant_id", "is_default"],
    )
    # `is_default` is a Python-side default on the model, so the server default
    # exists only long enough to create the column NOT NULL and is dropped to
    # keep `alembic check` quiet - the same pattern as 0031. `created_at` and
    # `updated_at` keep theirs, because `TimestampMixin` genuinely declares
    # `server_default=now()` and dropping them would be the drift.
    op.alter_column("payment_methods", "is_default", server_default=None)

    op.add_column(
        "payments",
        sa.Column("is_automatic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.alter_column("payments", "is_automatic", server_default=None)
    op.add_column(
        "payments",
        sa.Column("payment_method_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payments_payment_method_id_payment_methods",
        "payments",
        "payment_methods",
        ["payment_method_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "invoices",
        sa.Column("collection_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.alter_column("invoices", "collection_attempts", server_default=None)
    op.add_column(
        "invoices",
        sa.Column("next_collection_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoices", "next_collection_at")
    op.drop_column("invoices", "collection_attempts")

    op.drop_constraint(
        "fk_payments_payment_method_id_payment_methods",
        "payments",
        type_="foreignkey",
    )
    op.drop_column("payments", "payment_method_id")
    op.drop_column("payments", "is_automatic")

    op.drop_index("ix_payment_methods_tenant_id_is_default", table_name="payment_methods")
    op.drop_index("ix_payment_methods_tenant_id", table_name="payment_methods")
    op.drop_table("payment_methods")
    PAYMENT_METHOD_STATUS.drop(op.get_bind(), checkfirst=False)
