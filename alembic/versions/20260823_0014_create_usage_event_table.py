"""create the usage event table

Revision ID: 0014
Revises: 0013

One append-only table, and its indexes are the whole design.

`usage_events` is written on every message, every agent turn and every retrieval,
and read in aggregate. It will be the largest table in the schema within a month
of a workspace being busy, so every index here is a query that actually runs:

- `(tenant_id, occurred_at)` serves the dashboard summary, which sums every
  meter for one workspace over a window.
- `(tenant_id, event_type, occurred_at)` serves one meter at a time - a plan
  limit asking how many AI requests this month, a chart of messages per day.
- `(occurred_at)` serves the platform total, which spans every workspace and
  can therefore use no tenant-leading index at all.
- `(tenant_id)` on its own is the tenant-scope index every workspace-owned table
  carries; the model asserts its presence across the schema.

There is no `updated_at` because nothing updates a row. A correction is another
row, which is what keeps a past month's figure reproducible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

USAGE_EVENT_TYPE = postgresql.ENUM(
    "whatsapp_message_received",
    "whatsapp_message_sent",
    "ai_request",
    "ai_input_token",
    "ai_output_token",
    "rag_query",
    "media_processing",
    "voice_transcription",
    "storage_used",
    "lead_created",
    "conversation_created",
    "campaign_message",
    "api_request",
    name="usage_event_type",
    create_type=False,
)
USAGE_UNIT = postgresql.ENUM(
    "count",
    "token",
    "byte",
    "second",
    name="usage_unit",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()
    USAGE_EVENT_TYPE.create(bind, checkfirst=False)
    USAGE_UNIT.create(bind, checkfirst=False)

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", USAGE_EVENT_TYPE, nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit", USAGE_UNIT, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_usage_events_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
    )
    op.create_index("ix_usage_events_tenant_id", "usage_events", ["tenant_id"])
    op.create_index(
        "ix_usage_events_tenant_id_occurred_at",
        "usage_events",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_usage_events_tenant_id_event_type_occurred_at",
        "usage_events",
        ["tenant_id", "event_type", "occurred_at"],
    )
    op.create_index("ix_usage_events_occurred_at", "usage_events", ["occurred_at"])


def downgrade():
    bind = op.get_bind()

    op.drop_index("ix_usage_events_occurred_at", table_name="usage_events")
    op.drop_index(
        "ix_usage_events_tenant_id_event_type_occurred_at",
        table_name="usage_events",
    )
    op.drop_index("ix_usage_events_tenant_id_occurred_at", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_id", table_name="usage_events")
    op.drop_table("usage_events")

    USAGE_UNIT.drop(bind, checkfirst=False)
    USAGE_EVENT_TYPE.drop(bind, checkfirst=False)
