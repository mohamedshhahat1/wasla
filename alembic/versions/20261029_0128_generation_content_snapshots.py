"""Generation content snapshots and the recovery sweep's enqueue marker.

Revision ID: 0128
Revises: 0127

RAG remediation part 1 (RAG_SYSTEM_REVIEW.md, RQ-06, RQ-23, FX-2.1, FX-2.6, FX-1.9).

1. `document_index_generations` gains the content it indexes - `content`,
   `content_hash`, `title`, `filename`, `media_type`, `byte_size`, `source`,
   `extraction_warnings`, `original_object_key` - D1. A replacement carries its
   new version here while the document keeps serving the old one; the publish
   copies the snapshot onto `documents` in the swap transaction. All nullable:
   NULL is "index the document's stored text", which is what every generation
   made before this revision did and what a re-index still does.
2. `document_index_generations.last_enqueued_at` - D8, FX-1.9. When the recovery
   sweep last queued the attempt, so it does not queue it again within the
   queue's visibility timeout.

The failure code `replaced` (D2) needs nothing here: `last_error_code` is a
bounded string, not an enum.

**Online.** Nullable `ADD COLUMN`s without defaults - catalogue-only - under a
bounded `lock_timeout`. `source` reuses the existing `document_source` type.

**Downgrade** refuses while any generation holds a snapshot that is still in
flight or an original file key: dropping them would lose a replacement before
it published, or the only record of an object in the store.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "document_index_generations"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    source = postgresql.ENUM(name="document_source", create_type=False)
    for column in (
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("filename", sa.String(300), nullable=True),
        sa.Column("media_type", sa.String(150), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("source", source, nullable=True),
        sa.Column("extraction_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("original_object_key", sa.String(500), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column(TABLE, column)


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    for label, sql in (
        (
            "replacements still in flight",
            f"SELECT count(*) FROM {TABLE}"  # noqa: S608
            " WHERE content IS NOT NULL AND state IN ('pending', 'processing')",
        ),
        (
            "original files in the object store",
            f"SELECT count(*) FROM {TABLE} WHERE original_object_key IS NOT NULL",  # noqa: S608
        ),
    ):
        count = bind.exec_driver_sql(sql).scalar_one()
        if count:
            found.append(f"{count} {label}")
    if found:
        raise RuntimeError("Refusing to downgrade 0128: it would discard " + ", ".join(found) + ".")
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for name in (
        "last_enqueued_at",
        "original_object_key",
        "extraction_warnings",
        "source",
        "byte_size",
        "media_type",
        "filename",
        "title",
        "content_hash",
        "content",
    ):
        op.drop_column(TABLE, name)
