"""Financial rows name their own workspace, and settled history stays settled.

Revision ID: 0077
Revises: 0076

Two findings of the database audit, both "Python guarantees it, PostgreSQL
accepts the opposite":

**DB-004 - cross-workspace money.** Direct SQL bound one workspace's payment to
another's invoice, a top-up purchase to another's invoice, an incident to
another's payment, an automatic charge to another's saved card, and pinned a
subscription to a version of a different plan. The ledger now uses the
ADR-100 composite keys:

    payments            (tenant_id, invoice_id)         -> invoices        RESTRICT
    payments            (tenant_id, payment_method_id)  -> payment_methods SET NULL (column)
    invoices            (tenant_id, subscription_id)    -> subscriptions   SET NULL (column)
    topup_purchases     (tenant_id, invoice_id)         -> invoices        RESTRICT
    topup_purchases     (tenant_id, payment_id)         -> payments        RESTRICT
    billing_incidents   (tenant_id, invoice_id)         -> invoices        RESTRICT
    billing_incidents   (tenant_id, payment_id)         -> payments        RESTRICT
    billing_adjustments (tenant_id, invoice_id)         -> invoices        RESTRICT
    billing_adjustments (tenant_id, subscription_id)    -> subscriptions   SET NULL (column)
    subscriptions       (plan_id, plan_version_id)      -> plan_versions   RESTRICT

and a CHECK that an incident naming money names a workspace. The single-column
keys they supersede are dropped once the composites are validated.

**DB-005 - rewritable history.** A paid invoice's amounts, version and lines,
a succeeded payment's amount, `paid` without `paid_at`, `succeeded` without
`processed_at`, a paid invoice reopened with its money still on it, a top-up
granted on an unpaid invoice and a declined offer made active were all
accepted. Now: CHECKs for the two dated states; `invoices_history_immutable`
and `payments_history_immutable` (BEFORE UPDATE) freezing settled terms while
allowing the documented reversals, which always give money back; and two
commit-time checks - a purchased top-up is granted only on a paid invoice, an
offer is active only with a paid invoice.

**Online.** The composite targets' unique indexes are built `CONCURRENTLY`
first, outside any transaction, and attached as constraints afterwards. Keys
and CHECKs are added `NOT VALID` and validated in a separate statement, whose
lock does not block writes. **Nothing is repaired:** existing rows any new rule
would refuse are counted first and the migration fails, changing nothing
(docs/RUNBOOK.md, "Financial integrity (before and after deploying 0077)").
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

# The unique index each composite key's target needs: name, table, columns.
TARGETS = (
    ("uq_invoices_tenant_id_id", "invoices", "tenant_id, id"),
    ("uq_payments_tenant_id_id", "payments", "tenant_id, id"),
    ("uq_payment_methods_tenant_id_id", "payment_methods", "tenant_id, id"),
    ("uq_subscriptions_tenant_id_id", "subscriptions", "tenant_id, id"),
    ("uq_plan_versions_plan_id_id", "plan_versions", "plan_id, id"),
)

# name, child, child columns, parent, parent columns, on delete, superseded key
KEYS = (
    (
        "fk_payments_tenant_invoice",
        "payments",
        "tenant_id, invoice_id",
        "invoices",
        "tenant_id, id",
        "RESTRICT",
        "fk_payments_invoice_id_invoices",
    ),
    (
        "fk_payments_tenant_payment_method",
        "payments",
        "tenant_id, payment_method_id",
        "payment_methods",
        "tenant_id, id",
        "SET NULL (payment_method_id)",
        "fk_payments_payment_method_id_payment_methods",
    ),
    (
        "fk_invoices_tenant_subscription",
        "invoices",
        "tenant_id, subscription_id",
        "subscriptions",
        "tenant_id, id",
        "SET NULL (subscription_id)",
        "fk_invoices_subscription_id_subscriptions",
    ),
    (
        "fk_topup_purchases_tenant_invoice",
        "topup_purchases",
        "tenant_id, invoice_id",
        "invoices",
        "tenant_id, id",
        "RESTRICT",
        "fk_topup_purchases_invoice_id_invoices",
    ),
    (
        "fk_topup_purchases_tenant_payment",
        "topup_purchases",
        "tenant_id, payment_id",
        "payments",
        "tenant_id, id",
        "RESTRICT",
        "fk_topup_purchases_payment_id_payments",
    ),
    (
        "fk_billing_incidents_tenant_invoice",
        "billing_incidents",
        "tenant_id, invoice_id",
        "invoices",
        "tenant_id, id",
        "RESTRICT",
        "fk_billing_incidents_invoice_id_invoices",
    ),
    (
        "fk_billing_incidents_tenant_payment",
        "billing_incidents",
        "tenant_id, payment_id",
        "payments",
        "tenant_id, id",
        "RESTRICT",
        "fk_billing_incidents_payment_id_payments",
    ),
    (
        "fk_billing_adjustments_tenant_invoice",
        "billing_adjustments",
        "tenant_id, invoice_id",
        "invoices",
        "tenant_id, id",
        "RESTRICT",
        "fk_billing_adjustments_invoice_id_invoices",
    ),
    (
        "fk_billing_adjustments_tenant_subscription",
        "billing_adjustments",
        "tenant_id, subscription_id",
        "subscriptions",
        "tenant_id, id",
        "SET NULL (subscription_id)",
        "fk_billing_adjustments_subscription_id_subscriptions",
    ),
    (
        "fk_subscriptions_plan_version_of_plan",
        "subscriptions",
        "plan_id, plan_version_id",
        "plan_versions",
        "plan_id, id",
        "RESTRICT",
        "fk_subscriptions_plan_version_id_plan_versions",
    ),
)

CHECKS = (
    ("ck_invoices_paid_is_dated", "invoices", "status <> 'paid' OR paid_at IS NOT NULL"),
    (
        "ck_payments_collected_is_processed",
        "payments",
        "status NOT IN ('succeeded', 'refunded') OR processed_at IS NOT NULL",
    ),
    (
        "ck_billing_incidents_money_has_a_workspace",
        "billing_incidents",
        "tenant_id IS NOT NULL OR (payment_id IS NULL AND invoice_id IS NULL)",
    ),
)

PRECHECKS = (
    (
        "payments on another workspace's invoice",
        "SELECT count(*) FROM payments p JOIN invoices i ON i.id = p.invoice_id"
        " WHERE i.tenant_id <> p.tenant_id",
    ),
    (
        "payments on another workspace's card",
        "SELECT count(*) FROM payments p JOIN payment_methods m ON m.id = p.payment_method_id"
        " WHERE m.tenant_id <> p.tenant_id",
    ),
    (
        "invoices of another workspace's subscription",
        "SELECT count(*) FROM invoices i JOIN subscriptions s ON s.id = i.subscription_id"
        " WHERE s.tenant_id <> i.tenant_id",
    ),
    (
        "top-ups on another workspace's invoice",
        "SELECT count(*) FROM topup_purchases t JOIN invoices i ON i.id = t.invoice_id"
        " WHERE i.tenant_id <> t.tenant_id",
    ),
    (
        "top-ups on another workspace's payment",
        "SELECT count(*) FROM topup_purchases t JOIN payments p ON p.id = t.payment_id"
        " WHERE p.tenant_id <> t.tenant_id",
    ),
    (
        "incidents on another workspace's invoice",
        "SELECT count(*) FROM billing_incidents b JOIN invoices i ON i.id = b.invoice_id"
        " WHERE i.tenant_id IS DISTINCT FROM b.tenant_id",
    ),
    (
        "incidents on another workspace's payment",
        "SELECT count(*) FROM billing_incidents b JOIN payments p ON p.id = b.payment_id"
        " WHERE p.tenant_id IS DISTINCT FROM b.tenant_id",
    ),
    (
        "adjustments on another workspace's invoice",
        "SELECT count(*) FROM billing_adjustments a JOIN invoices i ON i.id = a.invoice_id"
        " WHERE i.tenant_id <> a.tenant_id",
    ),
    (
        "adjustments on another workspace's subscription",
        "SELECT count(*) FROM billing_adjustments a JOIN subscriptions s"
        " ON s.id = a.subscription_id WHERE s.tenant_id <> a.tenant_id",
    ),
    (
        "subscriptions pinned to a version of another plan",
        "SELECT count(*) FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id"
        " WHERE v.plan_id <> s.plan_id",
    ),
    (
        "paid invoices without paid_at",
        "SELECT count(*) FROM invoices WHERE status = 'paid' AND paid_at IS NULL",
    ),
    (
        "collected payments without processed_at",
        "SELECT count(*) FROM payments WHERE status IN ('succeeded', 'refunded')"
        " AND processed_at IS NULL",
    ),
    (
        "purchased top-ups granted on an unpaid invoice",
        "SELECT count(*) FROM topup_purchases t LEFT JOIN invoices i ON i.id = t.invoice_id"
        " WHERE t.source = 'purchase' AND t.status = 'granted'"
        " AND (i.id IS NULL OR i.status <> 'paid')",
    ),
    (
        "active offers without a paid invoice",
        "SELECT count(*) FROM custom_plan_offers o WHERE o.status = 'active' AND NOT EXISTS"
        " (SELECT 1 FROM invoices i WHERE i.custom_plan_offer_id = o.id AND i.status = 'paid')",
    ),
)

# Restated rather than imported, so this migration keeps meaning what it meant
# when the models move on.
INVOICE_HISTORY_FUNCTION = """
    CREATE OR REPLACE FUNCTION invoices_refuse_history_rewrite() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF OLD.status::text NOT IN ('paid', 'void') THEN
            RETURN NEW;
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.purpose IS DISTINCT FROM OLD.purpose
           OR NEW.plan_code IS DISTINCT FROM OLD.plan_code
           OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
           OR NEW.custom_plan_offer_id IS DISTINCT FROM OLD.custom_plan_offer_id
           OR NEW.amount_due IS DISTINCT FROM OLD.amount_due
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.lines IS DISTINCT FROM OLD.lines
           OR NEW.period_start IS DISTINCT FROM OLD.period_start
           OR NEW.period_end IS DISTINCT FROM OLD.period_end
           OR (NEW.subscription_id IS DISTINCT FROM OLD.subscription_id
               AND NEW.subscription_id IS NOT NULL) THEN
            RAISE EXCEPTION 'a settled invoice keeps the terms it was settled on'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.status::text = 'void'
           AND (NEW.status IS DISTINCT FROM OLD.status
                OR NEW.amount_paid IS DISTINCT FROM OLD.amount_paid) THEN
            RAISE EXCEPTION 'a void invoice stays void'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.status::text = 'paid' THEN
            IF NEW.amount_paid > OLD.amount_paid THEN
                RAISE EXCEPTION 'a paid invoice takes no more money'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status::text <> 'paid' AND NEW.amount_paid >= OLD.amount_paid THEN
                RAISE EXCEPTION 'a paid invoice reopens only when money goes back'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status::text = 'paid' AND NEW.paid_at IS DISTINCT FROM OLD.paid_at THEN
                RAISE EXCEPTION 'a paid invoice keeps when it was paid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$
    """
INVOICE_HISTORY_TRIGGER = "CREATE TRIGGER invoices_history_immutable BEFORE UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION invoices_refuse_history_rewrite()"
PAYMENT_HISTORY_FUNCTION = """
    CREATE OR REPLACE FUNCTION payments_refuse_history_rewrite() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF OLD.status::text NOT IN ('succeeded', 'refunded') THEN
            RETURN NEW;
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.provider IS DISTINCT FROM OLD.provider
           OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference
           OR NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id
           OR NEW.provider_integration_id IS DISTINCT FROM OLD.provider_integration_id
           OR NEW.processed_at IS DISTINCT FROM OLD.processed_at
           OR NEW.is_automatic IS DISTINCT FROM OLD.is_automatic
           OR NEW.manual_reference IS DISTINCT FROM OLD.manual_reference
           OR (OLD.applied_at IS NOT NULL AND NEW.applied_at IS DISTINCT FROM OLD.applied_at)
           OR (NEW.payment_method_id IS DISTINCT FROM OLD.payment_method_id
               AND NEW.payment_method_id IS NOT NULL) THEN
            RAISE EXCEPTION 'collected money keeps what it was'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status::text NOT IN ('succeeded', 'refunded')
           OR (OLD.status::text = 'refunded' AND NEW.status::text <> 'refunded')
           OR NEW.refunded_amount < OLD.refunded_amount THEN
            RAISE EXCEPTION 'collected money only ever goes back'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
PAYMENT_HISTORY_TRIGGER = "CREATE TRIGGER payments_history_immutable BEFORE UPDATE ON payments FOR EACH ROW EXECUTE FUNCTION payments_refuse_history_rewrite()"
TOPUP_GRANT_PAID_FUNCTION = """
    CREATE OR REPLACE FUNCTION topup_purchases_refuse_unpaid_grant() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM topup_purchases t
              LEFT JOIN invoices i ON i.id = t.invoice_id
             WHERE t.id = NEW.id
               AND t.source::text = 'purchase'
               AND t.status::text = 'granted'
               AND (i.id IS NULL OR i.status::text <> 'paid')
        ) THEN
            RAISE EXCEPTION 'a purchased top-up is granted only once its invoice is paid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
TOPUP_GRANT_PAID_TRIGGER = "CREATE CONSTRAINT TRIGGER topup_purchases_grant_paid AFTER INSERT OR UPDATE OF status ON topup_purchases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.status::text = 'granted' AND NEW.source::text = 'purchase') EXECUTE FUNCTION topup_purchases_refuse_unpaid_grant()"
OFFER_ACTIVE_PAID_FUNCTION = """
    CREATE OR REPLACE FUNCTION custom_plan_offers_refuse_unpaid_activation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM custom_plan_offers o
             WHERE o.id = NEW.id AND o.status::text = 'active'
               AND NOT EXISTS (
                   SELECT 1 FROM invoices i
                    WHERE i.custom_plan_offer_id = o.id AND i.status::text = 'paid')
        ) THEN
            RAISE EXCEPTION 'a custom plan offer is active only once its invoice is paid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
OFFER_ACTIVE_PAID_TRIGGER = "CREATE CONSTRAINT TRIGGER custom_plan_offers_active_paid AFTER INSERT OR UPDATE OF status ON custom_plan_offers DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.status::text = 'active') EXECUTE FUNCTION custom_plan_offers_refuse_unpaid_activation()"

# trigger, table, function - for the downgrade
TRIGGERS = (
    ("invoices_history_immutable", "invoices", "invoices_refuse_history_rewrite()"),
    ("payments_history_immutable", "payments", "payments_refuse_history_rewrite()"),
    ("topup_purchases_grant_paid", "topup_purchases", "topup_purchases_refuse_unpaid_grant()"),
    (
        "custom_plan_offers_active_paid",
        "custom_plan_offers",
        "custom_plan_offers_refuse_unpaid_activation()",
    ),
)


def _refuse_existing_violations() -> None:
    connection = op.get_bind()
    found = []
    for label, query in PRECHECKS:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            "Financial rows exist that the new keys and rules would refuse. They are "
            "evidence and must be investigated before this migration can run "
            "(docs/RUNBOOK.md, 'Financial integrity'). Nothing has been changed:\n  "
            + "\n  ".join(found)
        )


def _build_unique_index(name: str, table: str, columns: str) -> None:
    """CONCURRENTLY, dropping an INVALID leftover of a failed build first."""
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
                " WHERE c.relname = :name AND NOT x.indisvalid"
            ),
            {"name": name},
        )
        .scalar_one()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns})")


def upgrade() -> None:
    _refuse_existing_violations()

    with op.get_context().autocommit_block():
        for name, table, columns in TARGETS:
            _build_unique_index(name, table, columns)

    for name, table, _ in TARGETS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE USING INDEX {name}")
    for name, child, columns, parent, parent_columns, ondelete, _ in KEYS:
        op.execute(
            f"ALTER TABLE {child} ADD CONSTRAINT {name} FOREIGN KEY ({columns})"
            f" REFERENCES {parent} ({parent_columns}) ON DELETE {ondelete} NOT VALID"
        )
    for name, table, condition in CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")
    for name, child, *_ in KEYS:
        op.execute(f"ALTER TABLE {child} VALIDATE CONSTRAINT {name}")
    for name, table, _ in CHECKS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
    for _, child, *_rest, superseded in KEYS:
        op.drop_constraint(superseded, child, type_="foreignkey")

    for statement in (
        INVOICE_HISTORY_FUNCTION,
        INVOICE_HISTORY_TRIGGER,
        PAYMENT_HISTORY_FUNCTION,
        PAYMENT_HISTORY_TRIGGER,
        TOPUP_GRANT_PAID_FUNCTION,
        TOPUP_GRANT_PAID_TRIGGER,
        OFFER_ACTIVE_PAID_FUNCTION,
        OFFER_ACTIVE_PAID_TRIGGER,
    ):
        op.execute(statement)


def downgrade() -> None:
    """Back to 0076: the single-column keys return; no data is written."""
    for trigger, table, function in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    for name, table, _ in CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
    for name, child, columns, parent, _, ondelete, superseded in KEYS:
        column = columns.split(", ")[-1]
        single = "SET NULL" if ondelete.startswith("SET NULL") else ondelete
        op.create_foreign_key(superseded, child, parent, [column], ["id"], ondelete=single)
        op.drop_constraint(name, child, type_="foreignkey")
    for name, table, _ in TARGETS:
        op.drop_constraint(name, table, type_="unique")
