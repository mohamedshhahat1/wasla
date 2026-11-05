"""Per-workspace query spaces and shadow generations for an embedding-model switch.

Revision ID: 0136
Revises: 0135

RAG remediation part 3 (RQ-25, FX-4.1, RV-11, D11).

1. `document_generation_state` gains `shadow`, appended in an autocommit block:
   a generation built in a switch's target space, published and not served.
   `active` keeps its meaning and both partial unique indexes are unchanged.
   `audit_action` gains `knowledge_embedding_switch`.
2. `workspace_embedding_spaces`: one row per workspace that has started a
   switch - the space its searches embed in, and the space a switch under way
   is building in. No row is "the deployment's configured space", which is
   every workspace at this revision.
3. `shadow_has_identity`: a published shadow records its space, as an active or
   superseded generation does. Added `NOT VALID`, then validated - no row can
   be `shadow` yet, and validation takes only a `SHARE UPDATE EXCLUSIVE` lock.

**Online.** A new table, an enum label and a check constraint validated
without blocking writes; every statement under a bounded `lock_timeout`.

**Downgrade** refuses while any generation is a shadow (published or in
flight) or any workspace has a query-space row: the code a downgrade returns to
would serve every flipped workspace from the configured space - the outage this
migration exists to prevent. The `shadow` label stays (labels cannot be
dropped), unused.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABEL = "knowledge_embedding_switch"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_generation_state ADD VALUE IF NOT EXISTS 'shadow'")
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{LABEL}'")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        "workspace_embedding_spaces",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_provider", sa.String(length=32), nullable=False),
        sa.Column("query_model", sa.String(length=100), nullable=False),
        sa.Column("query_dimensions", sa.Integer(), nullable=False),
        sa.Column("query_schema_version", sa.Integer(), nullable=False),
        sa.Column("target_provider", sa.String(length=32), nullable=True),
        sa.Column("target_model", sa.String(length=100), nullable=True),
        sa.Column("target_dimensions", sa.Integer(), nullable=True),
        sa.Column("target_schema_version", sa.Integer(), nullable=True),
        sa.Column("switch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(target_provider IS NULL) = (target_model IS NULL)"
            " AND (target_provider IS NULL) = (target_dimensions IS NULL)"
            " AND (target_provider IS NULL) = (target_schema_version IS NULL)",
            name=op.f("ck_workspace_embedding_spaces_target_complete_or_absent"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_workspace_embedding_spaces_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_workspace_embedding_spaces")),
    )
    op.create_index(
        "ix_workspace_embedding_spaces_tenant_id", "workspace_embedding_spaces", ["tenant_id"]
    )
    op.execute(
        "ALTER TABLE document_index_generations ADD CONSTRAINT"
        " ck_document_index_generations_shadow_has_identity CHECK ("
        "state <> 'shadow' OR (published_at IS NOT NULL AND embedding_provider IS NOT NULL"
        " AND embedding_model IS NOT NULL AND embedding_dimensions IS NOT NULL"
        " AND embedding_schema_version IS NOT NULL)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE document_index_generations"
        " VALIDATE CONSTRAINT ck_document_index_generations_shadow_has_identity"
    )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    shadows = bind.exec_driver_sql(
        "SELECT count(*) FROM document_index_generations WHERE state::text = 'shadow'"
        " OR (trigger = 'embedding_switch' AND state::text IN ('pending', 'processing'))"
    ).scalar_one()
    if shadows:
        found.append(f"{shadows} shadow generations of an embedding-model switch")
    rows = bind.exec_driver_sql("SELECT count(*) FROM workspace_embedding_spaces").scalar_one()
    if rows:
        found.append(f"{rows} workspaces' query spaces")
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text = '{LABEL}'"  # noqa: S608
    ).scalar_one()
    if used:
        found.append(f"{used} audit rows recording embedding-model switches")
    if found:
        raise RuntimeError("Refusing to downgrade 0136: it would discard " + ", ".join(found) + ".")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        "ALTER TABLE document_index_generations"
        " DROP CONSTRAINT ck_document_index_generations_shadow_has_identity"
    )
    op.drop_index(
        "ix_workspace_embedding_spaces_tenant_id", table_name="workspace_embedding_spaces"
    )
    op.drop_table("workspace_embedding_spaces")
