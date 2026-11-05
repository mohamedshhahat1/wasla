"""Knowledge text structure: extraction warnings and heading breadcrumbs.

Revision ID: 0127
Revises: 0126

RAG remediation part 1 (RAG_SYSTEM_REVIEW.md).

1. `documents.extraction_warnings` (JSONB, nullable) - D4, RQ-04, RV-04. A PDF
   line the extractor may have re-ordered is counted per warning code, never
   copied: `{"digits_glued_to_arabic": 3}`. NULL for a document with none, and
   for every document stored before this revision.
2. `document_chunks.heading_path` (text, nullable) - D6, FX-1.2, RV-09. The
   section headings a chunk sits under, `"Pricing > Premium"`, so a passage cut
   from the middle of a section still says which section it is. NULL for
   chunks written before this revision; a re-index writes it.

**Online.** Two nullable `ADD COLUMN`s without defaults: catalogue-only changes
under a bounded `lock_timeout`, no rewrite of either table.

**Downgrade** refuses while any document carries warnings or any chunk a
heading path, rather than discard what an extraction or a chunking recorded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "documents",
        sa.Column("extraction_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("document_chunks", sa.Column("heading_path", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    for label, sql in (
        (
            "documents with extraction warnings",
            "SELECT count(*) FROM documents WHERE extraction_warnings IS NOT NULL",
        ),
        (
            "chunks with a heading path",
            "SELECT count(*) FROM document_chunks WHERE heading_path IS NOT NULL",
        ),
    ):
        count = bind.exec_driver_sql(sql).scalar_one()
        if count:
            found.append(f"{count} {label}")
    if found:
        raise RuntimeError("Refusing to downgrade 0127: it would discard " + ", ".join(found) + ".")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_column("document_chunks", "heading_path")
    op.drop_column("documents", "extraction_warnings")
