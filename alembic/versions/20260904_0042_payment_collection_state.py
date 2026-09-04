"""Give an automatic collection attempt a state, so a charge cannot be sent twice.

Revision ID: 0042
Revises: 0041

`payments.status` described a collection attempt completely while the payment
row and the provider request became durable in the same transaction. It does
not any more. ADR-088 commits the attempt *before* Paymob is asked to move
money, which introduces a distinction `status` has no way to carry:

    pending, nothing sent yet      safe. Nobody has been asked for money.
    pending, something was sent    a card may already have been debited.

Those are the same value in the old encoding, and telling them apart is the
whole of the fix - the first may be closed and retried, the second may not be
touched until a callback or a lookup says what happened. So the state becomes
a column.

## What this adds

- `collection_state`, a native enum, NULL for every payment that is not an
  automatic collection attempt.
- `reconciled_at`, when the provider was last asked about an attempt. Written
  before the lookup, so it is the lease as well as the record.
- `uq_payments_unresolved_collection`, a **partial unique index on
  `invoice_id`** while the state is unresolved. This is the constraint that
  closes WSL-01: one invoice may have at most one attempt whose outcome nobody
  knows, so a second charge is refused by the database rather than by a
  service remembering to look.
- `ix_payments_unresolved_collection`, reconciliation's only query. Partial,
  and on a healthy deployment empty.
- `ck_payments_collection_state`, tying the column to `is_automatic` so no
  reader has to guess whether a NULL means "not automatic" or "written before
  this migration".

## The backfill

Every existing row is classified by two columns it already carries.

`is_automatic = false` keeps NULL: a hosted checkout is somebody at a payment
page, and none of this describes it.

`is_automatic = true` with a terminal status - succeeded, failed, refunded -
becomes `settled`. That is what those words already mean: somebody decided,
and the decision is on the row.

`is_automatic = true` and still `pending` becomes **`requested`**, which is
the conservative reading and deliberately so. Under the previous protocol such
a row committed alongside the provider call, so it exists because that call
was reached; and even where it is not - the account-cannot-do-this branch
wrote one with no reference - calling it `requested` costs a lookup that
returns "no such order" and an abandonment, while calling it `claimed` would
authorise a second charge for a request that may have landed. The migration
takes the answer whose worst case is a delay rather than a debit.

There is normally nothing in this class at all. The old code held the payment
row and the provider request in one transaction, so an unresolved automatic
attempt outliving its worker was precisely the state that could not be
recorded - which is what WSL-01 was.

## Ordering

The unique index is created *after* the backfill, not before. Existing data
can perfectly well contain two pending automatic attempts against one invoice
- that is the defect this migration exists to make impossible - and building
the index first would fail the upgrade on exactly the deployments that need
it most. Any such pair is left as data; the index refuses new ones. A
deployment that finds the create failing has two unresolved attempts on one
invoice and a duplicate charge to investigate, which is a thing to be told
about rather than a thing to migrate around.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

STATES = ("claimed", "requested", "settled", "abandoned")

# One expression, restated here rather than imported from the model: a
# migration has to keep working when the model moves on.
UNRESOLVED = "collection_state IN ('claimed', 'requested')"
STATE_INVARIANT = "(collection_state IS NULL) = (is_automatic IS FALSE)"


def upgrade() -> None:
    state = sa.Enum(*STATES, name="payment_collection_state")
    state.create(op.get_bind(), checkfirst=True)

    op.add_column("payments", sa.Column("collection_state", state, nullable=True))
    op.add_column(
        "payments",
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("""
        UPDATE payments
        SET collection_state = CASE
            WHEN status = 'pending' THEN 'requested'
            ELSE 'settled'
        END::payment_collection_state
        WHERE is_automatic IS TRUE
        """)

    op.create_index(
        "uq_payments_unresolved_collection",
        "payments",
        ["invoice_id"],
        unique=True,
        postgresql_where=sa.text(UNRESOLVED),
    )
    op.create_index(
        "ix_payments_unresolved_collection",
        "payments",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text(UNRESOLVED),
    )
    # Named bare, as the model names it: the metadata's `ck` convention turns
    # this into `ck_payments_collection_state`, and spelling the prefix here
    # would double it.
    op.create_check_constraint("collection_state", "payments", STATE_INVARIANT)


def downgrade() -> None:
    """Reversible, and it loses exactly one thing: which attempts were in flight.

    A row in `requested` describes a charge that may have been made and whose
    answer has not arrived. The previous release has no column for that and
    reads it as an ordinary `pending` payment, which is what it was before
    this migration existed - so the row goes back to behaving exactly as it
    did, including being eligible for another attempt. That is the defect
    returning with the schema that had it, not new damage.

    Nothing is deleted and no status is rewritten. `status` was never touched
    on the way up, so there is nothing to undo on the way down.
    """
    # Named bare on the way down too: the metadata convention prefixes it, and
    # spelling the prefix here asks PostgreSQL for ck_payments_ck_payments_...
    op.drop_constraint("collection_state", "payments", type_="check")
    op.drop_index("ix_payments_unresolved_collection", table_name="payments")
    op.drop_index("uq_payments_unresolved_collection", table_name="payments")
    op.drop_column("payments", "reconciled_at")
    op.drop_column("payments", "collection_state")
    sa.Enum(name="payment_collection_state").drop(op.get_bind(), checkfirst=True)
