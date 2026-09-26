"""Settlement backstop: the books balance at commit, and held money is explained.

Revision ID: 0074
Revises: 0073

The database audit (DB-001) reproduced two concurrent settlements of one
invoice both applying: two succeeded payments (198.00) against an invoice
recording 99.00 paid, and no incident. The application now serializes
settlement under one lock order; this migration is the database's own refusal
underneath it, so a path that forgets the lock fails at commit instead of
recording money twice.

**`payments.applied_at`.** When settlement applied a payment's money to its
invoice. NULL for an attempt that collected nothing and for collected money
that was *held* - a second payment for an already-paid invoice, a page paid
after its offer was declined - which always has a billing incident. A CHECK
allows it only on collected payments.

**Two deferred constraint triggers** (`invoices_collection_reconciles`,
`payments_collection_reconciles`), checked at commit for every invoice or
collected payment a transaction touched:

- ``invoices.amount_paid`` equals the net of the payments applied to it, with
  the invoice locked ``FOR NO KEY UPDATE`` before it is summed;
- a collected payment that was not applied has a billing incident.

**`payments.manual_reference` (DB-017).** An operator's reference for money
that arrived outside a processor used to be stored as the payment's
``provider_reference``, which is unique across the platform: two workspaces
recording "BT-1" collided and the second got a 500. It is now its own column,
unique within one workspace and method, and such a payment's
``provider_reference`` is generated. Existing rows are left as they are.

**Backfill, then refuse on what it cannot explain.** Collected payments are
marked applied unless an incident records them as held (the incident a
refused settlement raises names the payment and its own transaction). The
migration then checks both invariants over every existing row and, if any
invoice's money does not reconcile or any collected payment is neither applied
nor explained, fails **without changing anything** - naming the counts. A
double settlement left by DB-001 is exactly such a row, and it is evidence of
a customer charged twice: an operator reconciles it (docs/RUNBOOK.md,
"Settlement backstop") rather than a migration deciding which payment was the
duplicate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

# Restated rather than imported, so this migration keeps meaning what it meant
# when the models move on.
COLLECTION_RECONCILES_FUNCTION = """
    CREATE OR REPLACE FUNCTION billing_refuse_unreconciled_collection() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    DECLARE
        target uuid;
        payment_status text;
        payment_applied timestamptz;
        held numeric;
        applied numeric;
    BEGIN
        IF TG_TABLE_NAME = 'payments' THEN
            SELECT p.invoice_id, p.status::text, p.applied_at
              INTO target, payment_status, payment_applied
              FROM payments p WHERE p.id = NEW.id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            IF payment_status IN ('succeeded', 'refunded') AND payment_applied IS NULL
               AND NOT EXISTS (SELECT 1 FROM billing_incidents b WHERE b.payment_id = NEW.id) THEN
                RAISE EXCEPTION 'collected money not applied to its invoice is held by an incident'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            target := NEW.id;
        END IF;
        SELECT i.amount_paid INTO held FROM invoices i WHERE i.id = target FOR NO KEY UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        SELECT coalesce(sum(p.amount - p.refunded_amount), 0) INTO applied
          FROM payments p WHERE p.invoice_id = target AND p.applied_at IS NOT NULL;
        IF applied <> held THEN
            RAISE EXCEPTION 'an invoice holds exactly the money applied to it'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
INVOICES_TRIGGER = (
    "CREATE CONSTRAINT TRIGGER invoices_collection_reconciles "
    "AFTER INSERT OR UPDATE OF amount_paid ON invoices "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION billing_refuse_unreconciled_collection()"
)
PAYMENTS_TRIGGER = (
    "CREATE CONSTRAINT TRIGGER payments_collection_reconciles "
    "AFTER INSERT OR UPDATE OF status, amount, refunded_amount, applied_at, invoice_id "
    "ON payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "WHEN (NEW.status::text IN ('succeeded', 'refunded') OR NEW.applied_at IS NOT NULL) "
    "EXECUTE FUNCTION billing_refuse_unreconciled_collection()"
)

# A collected payment is held, not applied, when the incident a refused
# settlement raises (`InvoiceSettlement._refuse`) names it and its own
# transaction; every other collected payment was applied.
BACKFILL_APPLIED = """
    UPDATE payments p
       SET applied_at = coalesce(p.processed_at, p.updated_at)
     WHERE p.status IN ('succeeded', 'refunded')
       AND p.applied_at IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM billing_incidents b
            WHERE b.payment_id = p.id
              AND b.kind::text IN ('duplicate_payment', 'refused_settlement',
                                   'mismatched_callback', 'custom_plan_scope_mismatch',
                                   'topup_duplicate_payment')
              AND b.provider_transaction_id IS NOT DISTINCT FROM p.provider_reference
       )
"""

# What the triggers will enforce from now on, over what already exists.
PRECHECKS = (
    (
        "invoices whose amount_paid is not the net of their applied payments",
        """
        SELECT count(*) FROM invoices i
         WHERE i.amount_paid <> coalesce((
               SELECT sum(p.amount - p.refunded_amount) FROM payments p
                WHERE p.invoice_id = i.id AND p.applied_at IS NOT NULL), 0)
        """,
    ),
    (
        "collected payments neither applied nor held with an incident",
        """
        SELECT count(*) FROM payments p
         WHERE p.status IN ('succeeded', 'refunded') AND p.applied_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM billing_incidents b WHERE b.payment_id = p.id)
        """,
    ),
)


def _refuse_unreconciled_ledger() -> None:
    connection = op.get_bind()
    found = []
    for label, query in PRECHECKS:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            "The existing ledger does not reconcile, so the settlement backstop cannot be "
            "installed. These rows are evidence - most likely a customer charged twice by "
            "the concurrent settlement DB-001 describes - and an operator must reconcile them "
            "(docs/RUNBOOK.md, 'Settlement backstop'). Nothing has been changed:\n  "
            + "\n  ".join(found)
        )


def upgrade() -> None:
    op.add_column("payments", sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("manual_reference", sa.String(200), nullable=True))
    op.execute(BACKFILL_APPLIED)
    _refuse_unreconciled_ledger()

    op.create_check_constraint(
        op.f("ck_payments_applied_only_when_collected"),
        "payments",
        "applied_at IS NULL OR status IN ('succeeded', 'refunded')",
    )
    op.create_index(
        "uq_payments_tenant_id_provider_manual_reference",
        "payments",
        ["tenant_id", "provider", "manual_reference"],
        unique=True,
        postgresql_where=sa.text("manual_reference IS NOT NULL"),
    )
    op.execute(COLLECTION_RECONCILES_FUNCTION)
    op.execute(INVOICES_TRIGGER)
    op.execute(PAYMENTS_TRIGGER)


def downgrade() -> None:
    """Back to 0073: the backstop goes, and with it the record of what was held.

    Refused while any manual payment carries an operator reference, because
    0073 has nowhere to keep it and dropping it would lose what an operator
    wrote down about real money. `applied_at` is derived and can go: at 0073
    held and applied money are distinguished by the incidents alone, as they
    were.
    """
    kept = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM payments WHERE manual_reference IS NOT NULL")
        .scalar_one()
    )
    if kept:
        raise RuntimeError(
            f"{kept} payment(s) carry an operator's manual reference that 0073 cannot hold. "
            "Refusing to drop it; nothing has been changed."
        )
    op.execute("DROP TRIGGER IF EXISTS payments_collection_reconciles ON payments")
    op.execute("DROP TRIGGER IF EXISTS invoices_collection_reconciles ON invoices")
    op.execute("DROP FUNCTION IF EXISTS billing_refuse_unreconciled_collection()")
    op.drop_index("uq_payments_tenant_id_provider_manual_reference", table_name="payments")
    op.drop_constraint(op.f("ck_payments_applied_only_when_collected"), "payments", type_="check")
    op.drop_column("payments", "manual_reference")
    op.drop_column("payments", "applied_at")
