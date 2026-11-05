"""Autovacuum and planner statistics sized for the knowledge chunks table.

Revision ID: 0137
Revises: 0136

RAG remediation part 3 (FX-4.2, FX-4.7, D12).

`document_chunks` is the table a re-index rewrites wholesale: every publish
deletes a generation's chunks and inserts the next one's, and `reindex-all` or
an embedding-model switch does that for every document. With PostgreSQL's
defaults autovacuum waits for a fifth of the table to be dead before it runs,
and until it does the deleted rows stay in the HNSW graph and spend an
approximate search's candidate budget - recall drops after a mass re-index
until the table is vacuumed (RAG-14). So this table is vacuumed and analysed
after 2 % of it changes (plus a floor of 1,000 rows), and after 5 % is newly
inserted.

`tenant_id`'s statistics target is raised to 1,000: whether the planner takes
the vector index or an exact scan for a workspace depends on its estimate of
that workspace's share of the table (ADR-079), and the default sample sees too
few workspaces once there are thousands.

**Online.** Storage parameters and a statistics target are catalogue changes
under a `SHARE UPDATE EXCLUSIVE` lock - reads and writes continue - with a
bounded `lock_timeout`. No rewrite. The new statistics apply at the next
`ANALYZE`, which the new thresholds bring sooner.

**Downgrade** resets both to PostgreSQL's defaults; it loses nothing.
"""

from __future__ import annotations

from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
PARAMETERS = (
    "autovacuum_vacuum_scale_factor = 0.02",
    "autovacuum_vacuum_threshold = 1000",
    "autovacuum_analyze_scale_factor = 0.02",
    "autovacuum_analyze_threshold = 1000",
    "autovacuum_vacuum_insert_scale_factor = 0.05",
)


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE document_chunks SET ({', '.join(PARAMETERS)})")
    op.execute("ALTER TABLE document_chunks ALTER COLUMN tenant_id SET STATISTICS 1000")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    names = ", ".join(parameter.split(" = ")[0] for parameter in PARAMETERS)
    op.execute(f"ALTER TABLE document_chunks RESET ({names})")
    op.execute("ALTER TABLE document_chunks ALTER COLUMN tenant_id SET STATISTICS -1")
