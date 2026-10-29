"""Stable document identity: a source key per knowledge base.

Revision ID: 0129
Revises: 0128

RAG remediation part 1 (RQ-07, FX-2.2, RV-02).

1. `documents.source_key` (nullable) - D3. A client-given key, or one derived
   from the filename, that makes "upload pricing.pdf again" a new version of
   the same document rather than a second document.
2. `uq_documents_tenant_kb_source_key` on `(tenant_id, knowledge_base_id,
   source_key) WHERE source_key IS NOT NULL`, built `CONCURRENTLY` in an
   autocommit block with an INVALID leftover dropped first.

Existing rows keep NULL here; `backfill-normalised-content` derives a key from
the filename where it is unique within its knowledge base, and lists the
collisions instead of choosing between them.

**Online.** A nullable `ADD COLUMN` under a bounded `lock_timeout`; the index
concurrently.

**Downgrade** refuses while any document has a source key.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
INDEX = "uq_documents_tenant_kb_source_key"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("documents", sa.Column("source_key", sa.String(300), nullable=True))
    op.execute("RESET lock_timeout")
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        invalid = bind.exec_driver_sql(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608
            f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
        ).first()
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX}"
            " ON documents (tenant_id, knowledge_base_id, source_key)"
            " WHERE source_key IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.exec_driver_sql(
        "SELECT count(*) FROM documents WHERE source_key IS NOT NULL"
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"Refusing to downgrade 0129: it would discard {count} document source keys."
        )
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("documents", "source_key")
