"""A workspace's own knowledge-retrieval switches.

Revision ID: 0134
Revises: 0133

RAG remediation part 2 (G9, RQ-13, RQ-16, RQ-19, RQ-20).

1. `tenants.knowledge_settings` (JSONB, nullable). A workspace's overrides of
   the deployment's retrieval defaults - hybrid search, re-rank, confidence
   tiers, number grounding, pre-retrieval, parallel sentiment - written by
   `PATCH /knowledge/settings`. NULL means "the deployment's defaults", which
   is every workspace at this revision.
2. `audit_action` gains `knowledge_settings_updated`, appended in an autocommit
   block as every label is.

**Online.** A nullable `ADD COLUMN` without a default is catalogue-only under a
bounded `lock_timeout`; `ADD VALUE` takes no table lock.

**Downgrade** refuses while any workspace has settings or any audit row uses
the label: PostgreSQL cannot drop an enum label, and the code a downgrade
returns to would ignore settings a workspace chose without saying so.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABEL = "knowledge_settings_updated"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "tenants",
        sa.Column("knowledge_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{LABEL}'")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    settings = bind.exec_driver_sql(
        "SELECT count(*) FROM tenants WHERE knowledge_settings IS NOT NULL"
    ).scalar_one()
    if settings:
        found.append(f"{settings} workspaces' knowledge settings")
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text = '{LABEL}'"  # noqa: S608
    ).scalar_one()
    if used:
        found.append(f"{used} audit rows recording knowledge settings changes")
    if found:
        raise RuntimeError("Refusing to downgrade 0134: it would discard " + ", ".join(found) + ".")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("tenants", "knowledge_settings")
