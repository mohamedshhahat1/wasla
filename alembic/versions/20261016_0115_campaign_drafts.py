"""Draft edits, create keys and the campaign activity's audit actions.

Revision ID: 0115
Revises: 0114

PG-02 .. PG-05, PG-08 (ADR-149).

1. `audit_action` gains `campaign_updated`, `campaign_duplicated`,
   `campaign_deleted`, `campaign_completed`, `campaign_failed`,
   `campaign_test_sent` and `campaign_recipients_exported`.
2. `uq_campaigns_tenant_id_id`, built `CONCURRENTLY` (a leftover INVALID build
   of a failed run dropped first, 0100's pattern): the target of a
   tenant-composite foreign key (ADR-100).
3. `campaigns.version` (integer, default 1): a draft's edit counter. A
   constant default is catalogue-only - no rewrite of `campaigns`.
4. `campaign_create_keys`: a create's `Idempotency-Key`, the draft it made and
   the request it was made with; the draft is one of the key's own workspace
   (`fk_campaign_create_keys_tenant_campaign`, cascading with the draft).

**Online.** The enum values and the unique index in an autocommit block
first; then a constant-default column and a new, empty table under a
bounded `lock_timeout`.

**Downgrade refuses** while any draft was edited (a version above 1), any
create key exists, or the trail records any of the new actions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABELS = (
    "campaign_updated",
    "campaign_duplicated",
    "campaign_deleted",
    "campaign_completed",
    "campaign_failed",
    "campaign_test_sent",
    "campaign_recipients_exported",
)
UNIQUE = "uq_campaigns_tenant_id_id"
TABLE = "campaign_create_keys"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")
        bind = op.get_bind()
        invalid = bind.exec_driver_sql(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constant
            f" WHERE c.relname = '{UNIQUE}' AND NOT i.indisvalid"
        ).scalar_one()
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {UNIQUE}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {UNIQUE} ON campaigns (tenant_id, id)"
        )

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "campaigns", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        TABLE,
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_create_keys"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_campaign_create_keys_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_campaign_create_keys_tenant_campaign",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "key", name="uq_campaign_create_keys_tenant_id_key"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_create_keys_tenant_id_id"),
    )
    op.create_index("ix_campaign_create_keys_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_campaign_create_keys_created_at", TABLE, ["created_at"])
    op.create_index("ix_campaign_create_keys_campaign_id", TABLE, ["campaign_id"])
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    edited = bind.exec_driver_sql("SELECT count(*) FROM campaigns WHERE version > 1").scalar_one()
    if edited:
        found.append(f"campaigns edited as drafts: {edited}")
    keys = bind.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one()  # noqa: S608
    if keys:
        found.append(f"campaign create keys: {keys}")
    labels = ", ".join(f"'{label}'" for label in LABELS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({labels})"  # noqa: S608 - module constants
    ).scalar_one()
    if audited:
        found.append(
            f"audit_logs recording a draft edit, copy, deletion, end, test or export: {audited}"
        )
    if found:
        raise RuntimeError(
            f"The campaign draft lifecycle cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table(TABLE)
    op.drop_column("campaigns", "version")
    op.execute(f"DROP INDEX IF EXISTS {UNIQUE}")
