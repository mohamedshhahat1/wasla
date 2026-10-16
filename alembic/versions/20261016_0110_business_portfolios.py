"""Meta's messaging limit, per business portfolio.

Revision ID: 0110
Revises: 0109

LIM-01 / LIM-02 / LIM-07 (ADR-146).

1. `whatsapp_business_portfolios` - platform-level, not tenant data: one row
   per portfolio Meta speaks of, keyed `(key_kind, meta_key)` - the WABA id,
   because Meta documents no field naming a WABA's portfolio - with Meta's
   value as given (`tier`), the unique people it allows per moving 24 hours
   (`messaging_limit`, null for unlimited), whether Wasla recognised the
   value, where it came from (`api` | `webhook`), when it was learned and
   when it last changed. `ck_..._read_complete` keeps a read's fields
   together; a never-read row has none of them.
2. `whatsapp_accounts.portfolio_id` -> `whatsapp_business_portfolios (id)`,
   `ON DELETE SET NULL`: the portfolio a number sends under. Added nullable,
   the key `NOT VALID` then validated; indexed `CONCURRENTLY` (a leftover
   INVALID build dropped first, 0100's pattern).

**Every existing number starts unknown.** No row is read here - a migration
does not call Meta. After this revision a campaign on a number whose limit was
never read is neither scheduled nor sent (fail closed); the deploy step reads
every connection's limit before the campaign workers start
(`scripts/read_messaging_limits.py`, docs/RUNBOOK.md).

**Online.** A new, empty table; a nullable column without a default; a key
validated under a bounded `lock_timeout`; an index built concurrently.

**Downgrade refuses** while any portfolio has been read or any number is
linked to one: the limit Wasla learned would be lost.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "whatsapp_business_portfolios"
ACCOUNT_FK = "fk_whatsapp_accounts_portfolio_id_whatsapp_business_portfolios"
INDEX = "ix_whatsapp_accounts_portfolio_id"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0119)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        TABLE,
        sa.Column("key_kind", sa.String(16), nullable=False),
        sa.Column("meta_key", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(40), nullable=True),
        sa.Column("messaging_limit", sa.Integer(), nullable=True),
        sa.Column("recognised", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(16), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_whatsapp_business_portfolios"),
        sa.UniqueConstraint("key_kind", "meta_key", name="uq_whatsapp_business_portfolios_key"),
        sa.CheckConstraint(
            "key_kind IN ('waba')", name=op.f("ck_whatsapp_business_portfolios_key_kind_known")
        ),
        sa.CheckConstraint(
            "source IS NULL OR source IN ('api', 'webhook')",
            name=op.f("ck_whatsapp_business_portfolios_source_known"),
        ),
        sa.CheckConstraint(
            "messaging_limit IS NULL OR messaging_limit > 0",
            name=op.f("ck_whatsapp_business_portfolios_limit_positive"),
        ),
        sa.CheckConstraint(
            "(checked_at IS NULL AND tier IS NULL AND source IS NULL AND messaging_limit IS NULL)"
            " OR (checked_at IS NOT NULL AND tier IS NOT NULL AND source IS NOT NULL)",
            name=op.f("ck_whatsapp_business_portfolios_read_complete"),
        ),
    )
    op.add_column(
        "whatsapp_accounts",
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"ALTER TABLE whatsapp_accounts ADD CONSTRAINT {ACCOUNT_FK}"
        f" FOREIGN KEY (portfolio_id) REFERENCES {TABLE} (id) ON DELETE SET NULL NOT VALID"
    )
    op.execute(f"ALTER TABLE whatsapp_accounts VALIDATE CONSTRAINT {ACCOUNT_FK}")
    op.execute("RESET lock_timeout")

    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constant
                f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
            )
            .scalar_one()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON whatsapp_accounts (portfolio_id)"
            " WHERE portfolio_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    read = bind.exec_driver_sql(
        f"SELECT count(*) FROM {TABLE} WHERE checked_at IS NOT NULL"  # noqa: S608 - module constant
    ).scalar_one()
    if read:
        found.append(f"business portfolios whose messaging limit was read: {read}")
    linked = bind.exec_driver_sql(
        "SELECT count(*) FROM whatsapp_accounts WHERE portfolio_id IS NOT NULL"
    ).scalar_one()
    if linked:
        found.append(f"numbers linked to a business portfolio: {linked}")
    if found:
        raise RuntimeError(
            f"Business portfolios cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE whatsapp_accounts DROP CONSTRAINT IF EXISTS {ACCOUNT_FK}")
    op.drop_column("whatsapp_accounts", "portfolio_id")
    op.drop_table(TABLE)
