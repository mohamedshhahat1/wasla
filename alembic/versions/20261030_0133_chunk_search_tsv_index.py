"""The GIN index the exact-word channel searches.

Revision ID: 0133
Revises: 0132

RAG remediation part 2 (RQ-15, FX-3.3, D7).

`ix_document_chunks_search_tsv` on `document_chunks (search_tsv)`, GIN, built
`CONCURRENTLY` in an autocommit block so writes to `document_chunks` - every
publish - continue while it builds, with an INVALID leftover of an interrupted
build dropped first. NULL `search_tsv` rows (chunks published before 0132) are
not in the index and cost it nothing.

The neighbour lookup (FX-3.11) needs no index of its own: it is an exact match
on `uq_document_chunks_tenant_id_generation_id_ordinal`, which exists.

**Downgrade** drops the index inside the run's transaction, never
`CONCURRENTLY` in an autocommit block, which would commit every downgrade step
above it and strand the stamp if a lower migration refuses (RR-14, MIG-0091).
"""

from __future__ import annotations

from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
INDEX = "ix_document_chunks_search_tsv"


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
            " ON document_chunks USING gin (search_tsv)"
        )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
