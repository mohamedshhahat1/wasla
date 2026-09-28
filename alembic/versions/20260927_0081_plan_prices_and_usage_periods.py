"""Plan prices, price-pinned subscriptions and invoices, and usage cycles.

Revision ID: 0081
Revises: 0080

ADR-116. A plan version is one set of entitlements; what a customer pays for it
is a `plan_prices` row - an amount per billing term, monthly or yearly - and a
version may have several. Four changes, one logical unit:

1. **`plan_prices`**, immutable but for retirement, one active row per version,
   interval, count and currency. A priced version publishes its own terms as
   its first price (trigger `plan_versions_publish_price`); a free version has
   none (trigger `plan_prices_guard`).
2. **Subscriptions pin a price** (`plan_price_id`, and `scheduled_plan_price_id`
   beside the scheduled version), each a price of the version beside it by a
   composite key. A live subscription to a priced version must name one - a
   deferred constraint trigger.
3. **Invoices pin a price** with a snapshot of its term (`billing_interval`,
   `interval_count`), and a trigger requires a priced purchase or renewal to
   charge exactly the price it names and never change it. A custom plan offer
   pins the price it offers, and its checkout invoice must sell that price.
4. **The usage cycle is separate from the billing term** (`usage_period_start`,
   `usage_period_end`), inside it by a CHECK.

**The backfill invents nothing.** Every existing priced version gets exactly one
price - the price, currency and interval it was published with - and every
subscription, scheduled change, invoice and offer that names a priced version is
pinned to that one price. No yearly price is created for any plan: yearly
pricing is configured by an operator through the platform API. Every existing
subscription's usage cycle is its current billing period, which is exactly how
usage was counted before, so monthly behaviour is unchanged.

**Nothing is repaired.** Rows the backfill could only map by guessing - an
invoice charging a different amount or currency than the version it names, an
invoice charging money for a free version, an offer of a free version - are
counted first and the migration fails without changing anything (docs/
RUNBOOK.md, "Plan prices (0081)").

Constraints are added `NOT VALID` and validated separately; the new indexes are
built `CONCURRENTLY` at the end, each dropping an INVALID leftover of its own
name first so a retry finishes the job.

**Downgrade** refuses while any subscription, scheduled change, invoice or offer
names a price other than its version's published terms (a yearly price added to
a monthly version, say): dropping the column would silently re-price it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

PRECHECKS = (
    (
        "priced purchases or renewals whose amount or currency is not their version's",
        "SELECT count(*) FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id"
        " WHERE i.purpose::text IN ('checkout', 'renewal', 'manual') AND v.price > 0"
        " AND (i.amount_due <> v.price OR i.currency <> v.currency)",
    ),
    (
        "invoices charging money for a free plan version",
        "SELECT count(*) FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id"
        " WHERE i.purpose::text IN ('checkout', 'renewal', 'manual') AND v.price <= 0"
        " AND i.amount_due > 0",
    ),
    (
        "custom plan offers of a free plan version",
        "SELECT count(*) FROM custom_plan_offers o JOIN plan_versions v"
        " ON v.id = o.plan_version_id WHERE v.price <= 0",
    ),
)

# A downgrade drops the price columns, after which every row falls back to its
# version's published terms. Refused while any row names something else.
DOWNGRADE_PRECHECKS = (
    (
        "subscriptions renewing at a price other than their version's published terms",
        "SELECT count(*) FROM subscriptions s JOIN plan_prices p ON p.id = s.plan_price_id"
        " JOIN plan_versions v ON v.id = p.plan_version_id"
        " WHERE p.amount <> v.price OR p.billing_interval <> v.interval OR p.interval_count <> 1",
    ),
    (
        "scheduled changes to a price other than their version's published terms",
        "SELECT count(*) FROM subscriptions s JOIN plan_prices p"
        " ON p.id = s.scheduled_plan_price_id JOIN plan_versions v ON v.id = p.plan_version_id"
        " WHERE p.amount <> v.price OR p.billing_interval <> v.interval OR p.interval_count <> 1",
    ),
    (
        "invoices charging a price other than their version's published terms",
        "SELECT count(*) FROM invoices i JOIN plan_prices p ON p.id = i.plan_price_id"
        " JOIN plan_versions v ON v.id = p.plan_version_id"
        " WHERE p.amount <> v.price OR p.billing_interval <> v.interval OR p.interval_count <> 1",
    ),
    (
        "offers of a price other than their version's published terms",
        "SELECT count(*) FROM custom_plan_offers o JOIN plan_prices p ON p.id = o.plan_price_id"
        " JOIN plan_versions v ON v.id = p.plan_version_id"
        " WHERE p.amount <> v.price OR p.billing_interval <> v.interval OR p.interval_count <> 1",
    ),
    (
        "annual subscriptions whose usage cycle is not their billing period",
        "SELECT count(*) FROM subscriptions WHERE ended_at IS NULL"
        " AND (usage_period_start <> current_period_start"
        " OR usage_period_end <> current_period_end)",
    ),
)

# ----------------------------------------------------------------- functions
# Restated verbatim from the models (`app/db/models/billing.py`, `invoice.py`,
# `custom_plan_offer.py`); `test_schema_parity.py` compares the bodies.

PLAN_PRICE_GUARD_FUNCTION = """
    CREATE OR REPLACE FUNCTION plan_prices_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF EXISTS (SELECT 1 FROM plan_versions v
                        WHERE v.id = NEW.plan_version_id AND v.price <= 0) THEN
                RAISE EXCEPTION 'a free plan version is not sold and has no prices'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
           OR NEW.billing_interval IS DISTINCT FROM OLD.billing_interval
           OR NEW.interval_count IS DISTINCT FROM OLD.interval_count
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.reason IS DISTINCT FROM OLD.reason THEN
            RAISE EXCEPTION 'plan_prices terms are immutable; retire the price and create another'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.retired_at IS NOT NULL AND (
               NEW.retired_at IS DISTINCT FROM OLD.retired_at
            OR NEW.retirement_reason IS DISTINCT FROM OLD.retirement_reason
            OR (NEW.retired_by IS DISTINCT FROM OLD.retired_by AND NEW.retired_by IS NOT NULL)
        ) THEN
            RAISE EXCEPTION 'a retired price stays retired'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
PLAN_PRICE_GUARD_TRIGGER = (
    "CREATE TRIGGER plan_prices_guard BEFORE INSERT OR UPDATE ON plan_prices "
    "FOR EACH ROW EXECUTE FUNCTION plan_prices_guard()"
)

PUBLISH_PRICE_FUNCTION = """
    CREATE OR REPLACE FUNCTION plan_versions_publish_price() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        INSERT INTO plan_prices (id, plan_version_id, billing_interval, interval_count,
                                 amount, currency, created_at, created_by, reason)
        VALUES (gen_random_uuid(), NEW.id, NEW.interval, 1, NEW.price, NEW.currency,
                NEW.created_at, NEW.created_by, 'Published with the plan version.');
        RETURN NULL;
    END;
    $$
    """
PUBLISH_PRICE_TRIGGER = (
    "CREATE TRIGGER plan_versions_publish_price AFTER INSERT ON plan_versions "
    "FOR EACH ROW WHEN (NEW.price > 0) EXECUTE FUNCTION plan_versions_publish_price()"
)

SUBSCRIPTION_PRICE_PIN_FUNCTION = """
    CREATE OR REPLACE FUNCTION subscriptions_refuse_unpriced_terms() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id
             WHERE s.id = NEW.id AND s.ended_at IS NULL
               AND s.plan_price_id IS NULL AND v.price > 0
        ) THEN
            RAISE EXCEPTION 'a subscription to a priced plan version renews at one of its prices'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM subscriptions s JOIN plan_versions v ON v.id = s.scheduled_plan_version_id
             WHERE s.id = NEW.id AND s.scheduled_plan_price_id IS NULL AND v.price > 0
        ) THEN
            RAISE EXCEPTION 'a scheduled change to a priced plan version names its price'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
SUBSCRIPTION_PRICE_PIN_TRIGGER = (
    "CREATE CONSTRAINT TRIGGER subscriptions_price_pinned "
    "AFTER INSERT OR UPDATE OF plan_version_id, plan_price_id, scheduled_plan_version_id, "
    "scheduled_plan_price_id, ended_at ON subscriptions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION subscriptions_refuse_unpriced_terms()"
)

INVOICE_PRICE_FUNCTION = """
    CREATE OR REPLACE FUNCTION invoices_refuse_price_mismatch() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND (
               NEW.plan_price_id IS DISTINCT FROM OLD.plan_price_id
            OR NEW.billing_interval IS DISTINCT FROM OLD.billing_interval
            OR NEW.interval_count IS DISTINCT FROM OLD.interval_count
            OR (NEW.plan_price_id IS NOT NULL AND (
                   NEW.amount_due IS DISTINCT FROM OLD.amount_due
                OR NEW.currency IS DISTINCT FROM OLD.currency))
        ) THEN
            RAISE EXCEPTION 'an invoice keeps the price it was issued at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'INSERT' AND NEW.plan_price_id IS NULL
           AND NEW.purpose::text IN ('checkout', 'renewal', 'manual')
           AND EXISTS (SELECT 1 FROM plan_versions v
                        WHERE v.id = NEW.plan_version_id AND v.price > 0) THEN
            RAISE EXCEPTION 'an invoice for a priced plan version names the price it charges'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.plan_price_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM plan_prices p
             WHERE p.id = NEW.plan_price_id
               AND p.amount = NEW.amount_due
               AND p.currency = NEW.currency
               AND p.billing_interval = NEW.billing_interval
               AND p.interval_count = NEW.interval_count
        ) THEN
            RAISE EXCEPTION 'an invoice charges exactly the price it names'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
INVOICE_PRICE_TRIGGER = (
    "CREATE TRIGGER invoices_price_snapshot BEFORE INSERT OR UPDATE OF "
    "plan_price_id, billing_interval, interval_count, amount_due, currency ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_price_mismatch()"
)

OFFER_INTEGRITY_FUNCTION = """
    CREATE OR REPLACE FUNCTION custom_plan_offers_refuse_foreign_or_changed() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND (
               NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
            OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
            OR NEW.plan_price_id IS DISTINCT FROM OLD.plan_price_id
        ) THEN
            RAISE EXCEPTION 'a custom plan offer keeps the terms it was made with'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM plans p JOIN plan_versions v ON v.plan_id = p.id
             WHERE p.id = NEW.plan_id
               AND v.id = NEW.plan_version_id
               AND p.scope = 'tenant'
               AND p.tenant_id = NEW.tenant_id
        ) THEN
            RAISE EXCEPTION 'custom_plan_not_available_for_workspace'
                USING ERRCODE = 'integrity_constraint_violation',
                      DETAIL = 'An offer names its own workspace''s custom plan and version.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
PREVIOUS_OFFER_INTEGRITY_FUNCTION = """
    CREATE OR REPLACE FUNCTION custom_plan_offers_refuse_foreign_or_changed() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND (
               NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
            OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
            OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
        ) THEN
            RAISE EXCEPTION 'a custom plan offer keeps the terms it was made with'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM plans p JOIN plan_versions v ON v.plan_id = p.id
             WHERE p.id = NEW.plan_id
               AND v.id = NEW.plan_version_id
               AND p.scope = 'tenant'
               AND p.tenant_id = NEW.tenant_id
        ) THEN
            RAISE EXCEPTION 'custom_plan_not_available_for_workspace'
                USING ERRCODE = 'integrity_constraint_violation',
                      DETAIL = 'An offer names its own workspace''s custom plan and version.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """

INVOICE_OFFER_FUNCTION = """
    CREATE OR REPLACE FUNCTION invoices_refuse_offer_mismatch() RETURNS trigger AS $$
    BEGIN
        IF NEW.custom_plan_offer_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM custom_plan_offers o
             WHERE o.id = NEW.custom_plan_offer_id
               AND o.plan_version_id = NEW.plan_version_id
               AND o.plan_price_id = NEW.plan_price_id
        ) THEN
            RAISE EXCEPTION 'an invoice for a custom plan offer sells the offered version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.custom_plan_offer_id IS DISTINCT FROM OLD.custom_plan_offer_id THEN
            RAISE EXCEPTION 'an invoice keeps the offer it was opened for'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
PREVIOUS_INVOICE_OFFER_FUNCTION = """
    CREATE OR REPLACE FUNCTION invoices_refuse_offer_mismatch() RETURNS trigger AS $$
    BEGIN
        IF NEW.custom_plan_offer_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM custom_plan_offers o
             WHERE o.id = NEW.custom_plan_offer_id
               AND o.plan_version_id = NEW.plan_version_id
        ) THEN
            RAISE EXCEPTION 'an invoice for a custom plan offer sells the offered version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.custom_plan_offer_id IS DISTINCT FROM OLD.custom_plan_offer_id THEN
            RAISE EXCEPTION 'an invoice keeps the offer it was opened for'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
INVOICE_OFFER_TRIGGER = (
    "CREATE TRIGGER invoices_custom_plan_offer BEFORE INSERT OR UPDATE OF "
    "custom_plan_offer_id, plan_version_id, plan_price_id ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_offer_mismatch()"
)
PREVIOUS_INVOICE_OFFER_TRIGGER = (
    "CREATE TRIGGER invoices_custom_plan_offer BEFORE INSERT OR UPDATE OF "
    "custom_plan_offer_id, plan_version_id ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_offer_mismatch()"
)

NEW_ACTIONS = ("billing_plan_price_created", "billing_plan_price_retired")

# ------------------------------------------------------ constraints, indexes

CONSTRAINTS = (
    (
        "subscriptions",
        "fk_subscriptions_plan_price_of_version",
        "FOREIGN KEY (plan_version_id, plan_price_id)"
        " REFERENCES plan_prices (plan_version_id, id) ON DELETE RESTRICT",
    ),
    (
        "subscriptions",
        "fk_subscriptions_scheduled_price_of_version",
        "FOREIGN KEY (scheduled_plan_version_id, scheduled_plan_price_id)"
        " REFERENCES plan_prices (plan_version_id, id) ON DELETE RESTRICT",
    ),
    (
        "subscriptions",
        "ck_subscriptions_price_pinned",
        "CHECK (plan_price_id IS NULL OR plan_version_id IS NOT NULL)",
    ),
    (
        "subscriptions",
        "ck_subscriptions_scheduled_price_pinned",
        "CHECK (scheduled_plan_price_id IS NULL OR scheduled_plan_version_id IS NOT NULL)",
    ),
    (
        "subscriptions",
        "ck_subscriptions_usage_period_within_term",
        "CHECK (ended_at IS NOT NULL OR (usage_period_end > usage_period_start"
        " AND usage_period_start >= current_period_start"
        " AND usage_period_end <= current_period_end))",
    ),
    (
        "invoices",
        "fk_invoices_plan_price_of_version",
        "FOREIGN KEY (plan_version_id, plan_price_id)"
        " REFERENCES plan_prices (plan_version_id, id) ON DELETE RESTRICT",
    ),
    (
        "invoices",
        "ck_invoices_price_pinned",
        "CHECK (plan_price_id IS NULL OR plan_version_id IS NOT NULL)",
    ),
    (
        "invoices",
        "ck_invoices_price_snapshot_complete",
        "CHECK ((plan_price_id IS NULL) = (billing_interval IS NULL)"
        " AND (plan_price_id IS NULL) = (interval_count IS NULL))",
    ),
    (
        "custom_plan_offers",
        "fk_custom_plan_offers_plan_price_of_version",
        "FOREIGN KEY (plan_version_id, plan_price_id)"
        " REFERENCES plan_prices (plan_version_id, id) ON DELETE RESTRICT",
    ),
)

INDEXES = (
    (
        "ix_subscriptions_usage_period_end",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_subscriptions_usage_period_end"
        " ON subscriptions (usage_period_end)",
    ),
    (
        "ix_subscriptions_plan_price_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_subscriptions_plan_price_id"
        " ON subscriptions (plan_price_id)",
    ),
    (
        "ix_subscriptions_scheduled_plan_price_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_subscriptions_scheduled_plan_price_id"
        " ON subscriptions (scheduled_plan_price_id) WHERE scheduled_plan_price_id IS NOT NULL",
    ),
    (
        "ix_invoices_plan_price_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_invoices_plan_price_id"
        " ON invoices (plan_price_id) WHERE plan_price_id IS NOT NULL",
    ),
    (
        "ix_custom_plan_offers_plan_price_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_custom_plan_offers_plan_price_id"
        " ON custom_plan_offers (plan_price_id)",
    ),
)


def _refuse(checks: tuple[tuple[str, str], ...], what: str) -> None:
    connection = op.get_bind()
    found = []
    for label, query in checks:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"{what} (docs/RUNBOOK.md, 'Plan prices (0081)'). Nothing has been changed:\n  "
            + "\n  ".join(found)
        )


def _drop_if_invalid(name: str) -> None:
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


def upgrade() -> None:
    _refuse(
        PRECHECKS,
        "Commercial history exists that could only be mapped to a price by guessing",
    )
    billing_interval = postgresql.ENUM(name="billing_interval", create_type=False)

    op.create_table(
        "plan_prices",
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_interval", billing_interval, nullable=False),
        sa.Column("interval_count", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retirement_reason", sa.String(500), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_prices")),
        sa.UniqueConstraint(
            "plan_version_id", "id", name=op.f("uq_plan_prices_plan_version_id_id")
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_plan_prices_plan_version_id_plan_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_plan_prices_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by"],
            ["users.id"],
            name=op.f("fk_plan_prices_retired_by_users"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_plan_prices_amount_positive")),
        sa.CheckConstraint(
            "interval_count > 0", name=op.f("ck_plan_prices_interval_count_positive")
        ),
        sa.CheckConstraint("currency = 'EGP'", name=op.f("ck_plan_prices_currency_supported")),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at",
            name=op.f("ck_plan_prices_retired_after_created"),
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_plan_prices_active_slot ON plan_prices"
        " (plan_version_id, billing_interval, interval_count, currency)"
        " WHERE retired_at IS NULL"
    )
    op.execute(PLAN_PRICE_GUARD_FUNCTION)
    op.execute(PLAN_PRICE_GUARD_TRIGGER)

    # Every priced version's own published terms, and nothing else.
    op.execute(
        "INSERT INTO plan_prices (id, plan_version_id, billing_interval, interval_count,"
        " amount, currency, created_at, created_by, reason)"
        " SELECT gen_random_uuid(), v.id, v.interval, 1, v.price, v.currency, v.created_at,"
        " v.created_by, 'Migrated from the plan version''s published terms (0081).'"
        " FROM plan_versions v WHERE v.price > 0"
    )
    # From here on the database does the same for every new priced version.
    op.execute(PUBLISH_PRICE_FUNCTION)
    op.execute(PUBLISH_PRICE_TRIGGER)

    op.add_column(
        "subscriptions", sa.Column("plan_price_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "subscriptions",
        sa.Column("scheduled_plan_price_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("usage_period_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("usage_period_end", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoices", sa.Column("plan_price_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("invoices", sa.Column("billing_interval", billing_interval, nullable=True))
    op.add_column("invoices", sa.Column("interval_count", sa.Integer(), nullable=True))
    op.add_column(
        "custom_plan_offers",
        sa.Column("plan_price_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # Each priced version has exactly one price at this point, so these joins
    # are one-to-one: every row is pinned to its version's published terms.
    op.execute(
        "UPDATE subscriptions s SET plan_price_id = p.id"
        " FROM plan_prices p WHERE p.plan_version_id = s.plan_version_id"
    )
    op.execute(
        "UPDATE subscriptions s SET scheduled_plan_price_id = p.id"
        " FROM plan_prices p WHERE p.plan_version_id = s.scheduled_plan_version_id"
    )
    # A monthly subscription's usage cycle is its billing period: exactly how
    # usage was counted before this migration.
    op.execute(
        "UPDATE subscriptions SET usage_period_start = current_period_start,"
        " usage_period_end = current_period_end"
    )
    op.alter_column("subscriptions", "usage_period_start", nullable=False)
    op.alter_column("subscriptions", "usage_period_end", nullable=False)
    op.execute(
        "UPDATE invoices i SET plan_price_id = p.id, billing_interval = p.billing_interval,"
        " interval_count = p.interval_count FROM plan_prices p"
        " WHERE p.plan_version_id = i.plan_version_id"
        " AND i.purpose::text IN ('checkout', 'renewal', 'manual')"
    )
    op.execute(
        "UPDATE custom_plan_offers o SET plan_price_id = p.id"
        " FROM plan_prices p WHERE p.plan_version_id = o.plan_version_id"
    )
    op.alter_column("custom_plan_offers", "plan_price_id", nullable=False)

    for table, name, definition in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition} NOT VALID")
    for table, name, _ in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")

    op.execute(SUBSCRIPTION_PRICE_PIN_FUNCTION)
    op.execute(SUBSCRIPTION_PRICE_PIN_TRIGGER)
    op.execute(INVOICE_PRICE_FUNCTION)
    op.execute(INVOICE_PRICE_TRIGGER)
    op.execute(OFFER_INTEGRITY_FUNCTION)
    op.execute(INVOICE_OFFER_FUNCTION)
    op.execute("DROP TRIGGER invoices_custom_plan_offer ON invoices")
    op.execute(INVOICE_OFFER_TRIGGER)

    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _drop_if_invalid(name)
            op.execute(statement)
        # Appended, like every label since 0037: a PostgreSQL enum cannot drop
        # one, so the downgrade leaves them and a re-upgrade finds them.
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    _refuse(
        DOWNGRADE_PRECHECKS,
        "Rows name prices a pre-0081 schema cannot represent; downgrading would re-price them",
    )
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    op.execute("DROP TRIGGER invoices_custom_plan_offer ON invoices")
    op.execute(PREVIOUS_INVOICE_OFFER_FUNCTION)
    op.execute(PREVIOUS_INVOICE_OFFER_TRIGGER)
    op.execute(PREVIOUS_OFFER_INTEGRITY_FUNCTION)
    op.execute("DROP TRIGGER invoices_price_snapshot ON invoices")
    op.execute("DROP FUNCTION invoices_refuse_price_mismatch()")
    op.execute("DROP TRIGGER subscriptions_price_pinned ON subscriptions")
    op.execute("DROP FUNCTION subscriptions_refuse_unpriced_terms()")

    for table, name, _ in reversed(CONSTRAINTS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.drop_column("custom_plan_offers", "plan_price_id")
    op.drop_column("invoices", "interval_count")
    op.drop_column("invoices", "billing_interval")
    op.drop_column("invoices", "plan_price_id")
    op.drop_column("subscriptions", "usage_period_end")
    op.drop_column("subscriptions", "usage_period_start")
    op.drop_column("subscriptions", "scheduled_plan_price_id")
    op.drop_column("subscriptions", "plan_price_id")

    op.execute("DROP TRIGGER plan_versions_publish_price ON plan_versions")
    op.execute("DROP FUNCTION plan_versions_publish_price()")
    op.drop_table("plan_prices")
    op.execute("DROP FUNCTION plan_prices_guard()")
