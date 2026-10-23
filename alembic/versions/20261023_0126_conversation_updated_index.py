"""The inbox's incremental read: conversations changed since a moment.

Revision ID: 0126
Revises: 0125

RT-04 (ADR-155). `GET /conversations?updated_since=` reads a workspace's
conversations by `updated_at`, which the application maintains: the column's
`onupdate` applies to every ORM flush and every Core `UPDATE` the application
issues on `conversations` - a new message (`_advance`), a status, an owner, an
assignee, a priority, a disclosure. **Application, not a trigger**: every
writer of the table is this application, the value is the same clock the rest
of the row uses, and a trigger would be a second definition of "changed" that
a migration-built and a model-built schema would have to agree on.

1. `ix_conversations_tenant_id_updated_at` on `(tenant_id, updated_at, id)`,
   built `CONCURRENTLY` in an autocommit block with an INVALID leftover dropped
   first.

**Online.** Index only; no lock beyond what `CREATE INDEX CONCURRENTLY` takes.

**Downgrade** drops the index: it holds no data.
"""

from __future__ import annotations

from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

INDEX = "ix_conversations_tenant_id_updated_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        invalid = bind.exec_driver_sql(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608
            f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
        ).first()
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX}"
            " ON conversations (tenant_id, updated_at, id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
