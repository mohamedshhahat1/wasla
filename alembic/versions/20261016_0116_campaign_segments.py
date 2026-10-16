"""Saved campaign segments, and the contact picker's indexes.

Revision ID: 0116
Revises: 0115

PG-06, PG-07 (ADR-149).

1. `campaign_segments`: a workspace's named audience rules (recency, lead
   statuses) - never contact ids (`ck_campaign_segments_rules_have_no_contacts`);
   `UNIQUE(tenant_id, name)`, `UNIQUE(tenant_id, id)`; an optional number of the
   same workspace (`fk_campaign_segments_tenant_account`, cascading).
2. `campaigns.audience_segment_id`: the segment a campaign's rules were copied
   from, of the same workspace (`fk_campaigns_tenant_audience_segment`, added
   `NOT VALID` then validated; no action on delete - the service clears the
   reference before deleting a segment). Partial index `CONCURRENTLY`.
3. The picker's orders, `CONCURRENTLY`:
   `ix_conversations_tenant_id_account_id_last_inbound_at` and
   `ix_contacts_tenant_id_last_seen_at`.

**Online.** A new table, a nullable column without a default, a key
validated under a bounded `lock_timeout`; three indexes built concurrently
(a leftover INVALID build of a failed run dropped first, 0100's pattern).

**Downgrade refuses** while any segment exists or any campaign names one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "campaign_segments"
SEGMENT_KEY = "fk_campaigns_tenant_audience_segment"
INDEXES = (
    (
        "ix_campaigns_audience_segment_id",
        "campaigns (audience_segment_id) WHERE audience_segment_id IS NOT NULL",
    ),
    (
        "ix_conversations_tenant_id_account_id_last_inbound_at",
        "conversations (tenant_id, account_id, last_inbound_at DESC NULLS LAST, contact_id)",
    ),
    (
        "ix_contacts_tenant_id_last_seen_at",
        "contacts (tenant_id, last_seen_at DESC NULLS LAST, id)",
    ),
)
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        TABLE,
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_segments"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_campaign_segments_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_campaign_segments_tenant_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_campaign_segments_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_campaign_segments_tenant_id_name"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_segments_tenant_id_id"),
        sa.CheckConstraint(
            "NOT (rules ? 'contact_ids')",
            name=op.f("ck_campaign_segments_rules_have_no_contacts"),
        ),
    )
    op.create_index("ix_campaign_segments_tenant_id", TABLE, ["tenant_id"])
    op.add_column(
        "campaigns",
        sa.Column("audience_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {SEGMENT_KEY}"
        " FOREIGN KEY (tenant_id, audience_segment_id)"
        f" REFERENCES {TABLE} (tenant_id, id) NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {SEGMENT_KEY}")
    op.execute("RESET lock_timeout")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, target in INDEXES:
            invalid = bind.exec_driver_sql(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constants
                f" WHERE c.relname = '{name}' AND NOT i.indisvalid"
            ).scalar_one()
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {target}")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    segments = bind.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one()  # noqa: S608
    if segments:
        found.append(f"saved segments: {segments}")
    used = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE audience_segment_id IS NOT NULL"
    ).scalar_one()
    if used:
        found.append(f"campaigns naming a segment: {used}")
    if found:
        raise RuntimeError(
            f"Saved segments cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for name, _ in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {SEGMENT_KEY}")
    op.drop_column("campaigns", "audience_segment_id")
    op.drop_table(TABLE)
