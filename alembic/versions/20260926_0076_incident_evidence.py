"""Billing incidents keep the evidence they were raised with.

Revision ID: 0076
Revises: 0075

The database audit (DB-006) found the records ADR-033 and ADR-112 rely on
rewritable at the database: an incident's amount, kind or detail could be
changed and the row deleted. Two layers close it:

- **Privileges**, for the runtime role, applied by
  `scripts/provision_runtime_db_role.py` on every deploy (they are cluster
  facts about a role this migration does not know the name of): `audit_logs`
  is `SELECT`/`INSERT` only, and `billing_incidents` loses `DELETE` and keeps
  `UPDATE` only on its resolution columns.
- **This trigger**, for every role: an incident's evidence - workspace, kind,
  dedupe key, payment, invoice, provider, transaction, amount, currency,
  detail, when it was raised - never changes, and a resolved incident stays
  resolved. Resolution itself (status, resolved_at, resolved_by,
  resolution_note) remains writable.

No existing row changes. Downgrade drops the trigger and function.
"""

from __future__ import annotations

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

# Restated rather than imported, so this migration keeps meaning what it meant
# when the models move on.
EVIDENCE_FUNCTION = """
    CREATE OR REPLACE FUNCTION billing_incidents_refuse_evidence_change() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
           OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
           OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
           OR NEW.provider IS DISTINCT FROM OLD.provider
           OR NEW.provider_transaction_id IS DISTINCT FROM OLD.provider_transaction_id
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.detail IS DISTINCT FROM OLD.detail
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'a billing incident keeps the evidence it was raised with'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.status::text = 'resolved' AND NEW.status::text <> 'resolved' THEN
            RAISE EXCEPTION 'a resolved billing incident stays resolved'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
EVIDENCE_TRIGGER = (
    "CREATE TRIGGER billing_incidents_evidence_immutable BEFORE UPDATE ON billing_incidents "
    "FOR EACH ROW EXECUTE FUNCTION billing_incidents_refuse_evidence_change()"
)


def upgrade() -> None:
    op.execute(EVIDENCE_FUNCTION)
    op.execute(EVIDENCE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS billing_incidents_evidence_immutable ON billing_incidents")
    op.execute("DROP FUNCTION IF EXISTS billing_incidents_refuse_evidence_change()")
