"""The knowledge-gap report: searches that found nothing, for a workspace's admins.

Revision ID: 0138
Revises: 0137

RAG remediation part 3 (RQ-27, FX-4.10, IMP-25, D10).

`knowledge_search_gaps`: one row per agent search that came back empty while
`RAG_GAP_REPORT_ENABLED` is on (off by default, until the Privacy audit). It
holds the query the *model* wrote - never the customer's message - its
language, the best distance and when. Tenant-keyed and cascading with the
workspace, erased by the workspace purge, and kept 30 days by the recovery
sweep's retention pass (`ix_knowledge_search_gaps_created_at`).

**Online.** A new, empty table.

**Downgrade** refuses while it holds a row: they are what a workspace's admins
would read, and dropping them is not this migration's decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        "knowledge_search_gaps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("best_distance", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_search_gaps_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_search_gaps")),
    )
    op.create_index("ix_knowledge_search_gaps_tenant_id", "knowledge_search_gaps", ["tenant_id"])
    op.create_index(
        "ix_knowledge_search_gaps_tenant_id_created_at",
        "knowledge_search_gaps",
        ["tenant_id", "created_at"],
    )
    op.create_index("ix_knowledge_search_gaps_created_at", "knowledge_search_gaps", ["created_at"])


def downgrade() -> None:
    rows = op.get_bind().exec_driver_sql("SELECT count(*) FROM knowledge_search_gaps").scalar_one()
    if rows:
        raise RuntimeError(
            f"Refusing to downgrade 0138: it would discard {rows} knowledge-gap report rows."
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table("knowledge_search_gaps")
