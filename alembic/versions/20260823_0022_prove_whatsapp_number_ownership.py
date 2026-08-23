"""record proof of ownership for a WhatsApp number, and let one be released

ADR-037. `phone_number_id` was unique platform-wide and nothing checked whether
the workspace claiming one controlled it. `ownership_verified_at` records that a
workspace proved control of a number to Meta at claim time; `verified_name`
keeps what Meta called the business at that moment.

Existing rows get NULL for both, and that is deliberate. They were claimed
before proof existed, and back-dating them would erase exactly the list an
operator needs in order to re-verify.

The uniqueness of `phone_number_id` changes from a `UNIQUE` constraint to a
**partial unique index** over live claims — `released_at IS NULL`. That is what
makes handing a number back possible without deleting anything: conversations,
messages, templates and campaigns all cascade from `whatsapp_accounts`, so the
old constraint forced a choice between never letting a number move and
destroying a customer's history.

The index is created **before** the constraint is dropped. In between, both hold,
which is a moment of redundancy rather than a moment where two workspaces could
claim the same number.

Downgrade note: restoring `UNIQUE(phone_number_id)` fails if any number has been
released and re-claimed, because the released row and the live one share a value
the old constraint forbids. There is no correct automatic answer — deleting
somebody's conversation history to satisfy a downgrade is not one — so it fails
loudly and an operator decides. PostgreSQL also cannot remove a value from an
enum type, so `released` and `whatsapp_account_released` are left behind.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

LIVE_NUMBER_INDEX = "uq_whatsapp_accounts_live_phone_number_id"
OLD_NUMBER_CONSTRAINT = "uq_whatsapp_accounts_phone_number_id"


def _add_enum_values(type_name: str, values: tuple[str, ...]) -> None:
    """Extend a native enum, outside the migration's transaction.

    `ADD VALUE` cannot be used later in the same transaction that added it, and
    Alembic runs a migration inside one. `autocommit_block` is the supported way
    round that. `IF NOT EXISTS` makes the step re-runnable after a partial
    failure, which matters precisely because this part is not covered by the
    surrounding transaction.
    """
    with op.get_context().autocommit_block():
        for value in values:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")


def upgrade() -> None:
    _add_enum_values("whatsapp_account_status", ("released",))
    _add_enum_values("audit_action", ("whatsapp_account_released",))

    op.add_column(
        "whatsapp_accounts",
        sa.Column("verified_name", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "whatsapp_accounts",
        sa.Column("ownership_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "whatsapp_accounts",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        LIVE_NUMBER_INDEX,
        "whatsapp_accounts",
        ["phone_number_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_constraint(OLD_NUMBER_CONSTRAINT, "whatsapp_accounts", type_="unique")


def downgrade() -> None:
    # Reverse order, and the constraint goes back before the index comes out,
    # for the same reason the index went in first above.
    op.create_unique_constraint(
        OLD_NUMBER_CONSTRAINT,
        "whatsapp_accounts",
        ["phone_number_id"],
    )
    op.drop_index(LIVE_NUMBER_INDEX, table_name="whatsapp_accounts")
    op.drop_column("whatsapp_accounts", "released_at")
    op.drop_column("whatsapp_accounts", "ownership_verified_at")
    op.drop_column("whatsapp_accounts", "verified_name")
