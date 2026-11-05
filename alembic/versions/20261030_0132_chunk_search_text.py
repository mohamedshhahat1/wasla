"""The exact-word channel's columns on document chunks.

Revision ID: 0132
Revises: 0131

RAG remediation part 2 (RQ-15, FX-3.3, RC-4).

1. `document_chunks.search_text` (text, nullable) - D6b. The chunk as the
   lexical channel reads it (`for_lexical`).
2. `document_chunks.search_tsv` (tsvector, nullable) - D7. Its lexemes, written
   at publish. A plain column, **not** a `GENERATED ... STORED` one: adding a
   stored generated column to an existing table rewrites it on PostgreSQL 16,
   and `document_chunks` is the largest table this system writes. It is also
   built from lexemes Python tokenised rather than by `to_tsvector('simple',
   ...)`, whose parser splits `bk-35` into `bk` and `-35` (RR-16), so a
   generated expression could not have produced it anyway.

Existing chunks keep NULL in both. They are still found by the vector channel;
`reindex-all` (Part 1) rebuilds every document through generations and fills
both columns in the same pass as the contextual embedding (RQ-14).

**Online.** Two nullable `ADD COLUMN`s without defaults: catalogue-only under a
bounded `lock_timeout`, no rewrite. The GIN index is migration 0133, built
`CONCURRENTLY`.

**Downgrade** refuses while any chunk carries lexical text, rather than discard
what a publish wrote.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("document_chunks", sa.Column("search_text", sa.Text(), nullable=True))
    op.add_column("document_chunks", sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.exec_driver_sql(
        "SELECT count(*) FROM document_chunks"
        " WHERE search_text IS NOT NULL OR search_tsv IS NOT NULL"
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"Refusing to downgrade 0132: it would discard the lexical text of {count} chunks."
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("document_chunks", "search_tsv")
    op.drop_column("document_chunks", "search_text")
