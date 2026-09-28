"""Periods run forwards, one default card, one account per address.

Revision ID: 0078
Revises: 0077

Three rules the application already keeps, now kept by the database too:

- **DB-012**: `subscriptions.current_period_end > current_period_start` while
  the subscription is live, and `invoices.period_end >= period_start`. The
  audit wrote both reversed with plain SQL. The calendar code is correct (1.16M
  property checks); this stops a repair script from being the exception. An
  ended subscription is exempt: ending one sets its period end to the moment
  service stopped, which for a period billed in advance can be at or before
  that period's start.
- **DB-018**: at most one *active default* card per workspace, as a partial
  unique index. The audit's reasoning, confirmed here by a race test: two
  first cards saved at once both read "no default yet" and both became one.
- **DB-025**: `lower(email)` unique on users, so a case variant of an existing
  address - tombstoned accounts included - is refused however it is written.

**Nothing is repaired.** Rows each rule would refuse are counted first and the
migration fails without changing anything, naming them (docs/RUNBOOK.md,
"Periods, default cards and addresses (0078)"). The two unique indexes are
built `CONCURRENTLY`; a failed build's INVALID leftover is dropped and rebuilt
on a retry. The CHECKs are added `NOT VALID` and validated separately.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

CHECKS = (
    (
        "ck_subscriptions_period_ordered",
        "subscriptions",
        "ended_at IS NOT NULL OR current_period_end > current_period_start",
    ),
    ("ck_invoices_period_ordered", "invoices", "period_end >= period_start"),
)

INDEXES = (
    (
        "uq_payment_methods_one_active_default",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_payment_methods_one_active_default"
        " ON payment_methods (tenant_id) WHERE is_default AND status = 'active'",
    ),
    (
        "uq_users_email_lower",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_users_email_lower"
        " ON users (lower(email))",
    ),
)

PRECHECKS = (
    (
        "subscriptions whose period ends before it starts",
        "SELECT count(*) FROM subscriptions"
        " WHERE ended_at IS NULL AND current_period_end <= current_period_start",
    ),
    (
        "invoices whose period ends before it starts",
        "SELECT count(*) FROM invoices WHERE period_end < period_start",
    ),
    (
        "workspaces with more than one active default card",
        "SELECT count(*) FROM (SELECT tenant_id FROM payment_methods"
        " WHERE is_default AND status = 'active' GROUP BY tenant_id HAVING count(*) > 1) d",
    ),
    (
        "addresses held by more than one account ignoring case",
        "SELECT count(*) FROM (SELECT lower(email) FROM users"
        " GROUP BY lower(email) HAVING count(*) > 1) d",
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
            "Rows exist that the new period, default-card or address rules would refuse. "
            "They must be reconciled first (docs/RUNBOOK.md, 'Periods, default cards and "
            "addresses'). Nothing has been changed:\n  " + "\n  ".join(found)
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
    _refuse_existing_violations()
    for name, table, condition in CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")
    for name, table, _ in CHECKS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
    with op.get_context().autocommit_block():
        for name, statement in INDEXES:
            _drop_if_invalid(name)
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    for name, table, _ in CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
