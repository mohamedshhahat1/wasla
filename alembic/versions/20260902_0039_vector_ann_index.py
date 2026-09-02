"""Give tenant-scoped retrieval an approximate index to reach for.

Revision ID: 0039
Revises: 0038

`document_chunks.embedding` had no index at all, so every knowledge search
computed a cosine distance for every retrievable chunk in the workspace and
sorted the lot. Measured on a 77,000-chunk corpus: a workspace holding 45,000 of
them answers a top-5 in ~187ms, and it grows linearly for ever. The same query
against this index answers in ~2ms (ADR-079).

**`hnsw`, not `ivfflat`.** IVFFlat needs a populated table to train its lists
against, and a knowledge base is not populated when a workspace is created -
it fills up over months. An index whose recall depends on when it was built is
an index that is wrong for most of this table's life, and rebuilding it on a
schedule is an operational commitment nobody asked for. HNSW has no training
step, so it is correct on an empty table and stays correct as one fills.

**`vector_cosine_ops`**, because `KnowledgeRepository.search` orders by `<=>`.
The opclass must match the operator or the index is dead weight the planner
never considers - which is the failure this migration would otherwise ship
silently, since the catalogue looks identical either way.

**Built `CONCURRENTLY`**, and that is why this migration steps outside
Alembic's transaction. A plain `CREATE INDEX` holds a lock that blocks writes
to `document_chunks` for the length of the build, and the build is minutes on a
corpus of any size - so an ordinary deployment would stop document ingestion
platform-wide while it ran. The cost of doing it concurrently is that a failed
build leaves the index behind marked invalid, which is why the drop below is
unconditional: a retry of this migration must build the index rather than adopt
a half-finished one.

No data change, and no query changes shape. Retrieval still asks for the
nearest chunks in one workspace; PostgreSQL now has a second way to answer.
"""

from __future__ import annotations

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

INDEX = "ix_document_chunks_embedding_hnsw"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {INDEX} ON document_chunks"
            " USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
