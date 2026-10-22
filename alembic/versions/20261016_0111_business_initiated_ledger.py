"""The ledger of business-initiated sends.

Revision ID: 0111
Revises: 0110

LIM-03 / LIM-04 (ADR-146). `business_initiated_sends`: one row per template
message sent to a person whose service window on that number was closed, on
the business portfolio it counts against - written in the transaction that
commits the send intent, after counting under the portfolio's advisory lock.
Platform-level (the count binds every workspace on a portfolio); `tenant_id`
cascades with the workspace, `campaign_id` and `message_id` are set null when
those go. `ck_..._source_known` holds the closed vocabulary.

**Online.** A new, empty table, created in the run's transaction; its five
indexes are built `CONCURRENTLY` in an autocommit block afterwards (a leftover
INVALID build of a failed run dropped first, 0100's pattern) - nothing on an
existing table is locked beyond the foreign keys' brief `SHARE ROW EXCLUSIVE`
on the referenced tables, under a bounded `lock_timeout`.

**Downgrade refuses** while the ledger holds a row: dropping it would forget
who a portfolio has already reached in the moving 24 hours, and the next
campaign would send past Meta's limit.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "business_initiated_sends"
SOURCES = ("campaign", "campaign_test", "manual_template", "follow_up", "other")
INDEXES = (
    ("ix_business_initiated_sends_portfolio_id_sent_at", "(portfolio_id, sent_at)", ""),
    (
        "ix_business_initiated_sends_portfolio_id_recipient_sent_at",
        "(portfolio_id, recipient, sent_at)",
        "",
    ),
    ("ix_business_initiated_sends_message_id", "(message_id)", " WHERE message_id IS NOT NULL"),
    ("ix_business_initiated_sends_tenant_id", "(tenant_id)", ""),
    ("ix_business_initiated_sends_sent_at", "(sent_at)", ""),
)
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0120)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        TABLE,
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("limit_at_send", sa.Integer(), nullable=True),
        sa.Column("budget_at_send", sa.Integer(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_business_initiated_sends"),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["whatsapp_business_portfolios.id"],
            name="fk_business_initiated_sends_portfolio_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_business_initiated_sends_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_business_initiated_sends_campaign_id_campaigns",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_business_initiated_sends_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "source IN (" + ", ".join(f"'{source}'" for source in SOURCES) + ")",
            name=op.f("ck_business_initiated_sends_source_known"),
        ),
        sa.CheckConstraint(
            "limit_at_send IS NULL OR limit_at_send > 0",
            name=op.f("ck_business_initiated_sends_limit_at_send_positive"),
        ),
        sa.CheckConstraint(
            "budget_at_send IS NULL OR budget_at_send >= 0",
            name=op.f("ck_business_initiated_sends_budget_at_send_non_negative"),
        ),
    )
    op.execute("RESET lock_timeout")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, columns, where in INDEXES:
            invalid = bind.exec_driver_sql(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constants
                f" WHERE c.relname = '{name}' AND NOT i.indisvalid"
            ).scalar_one()
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {TABLE} {columns}{where}"
            )


def downgrade() -> None:
    count = f"SELECT count(*) FROM {TABLE}"  # noqa: S608 - module constant
    rows = op.get_bind().exec_driver_sql(count).scalar_one()
    if rows:
        raise RuntimeError(
            f"The business-initiated ledger cannot be downgraded ({RUNBOOK}). "
            f"Nothing has been changed:\n  ledger rows: {rows}"
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table(TABLE)
