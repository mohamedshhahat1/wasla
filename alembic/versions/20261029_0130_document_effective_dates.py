"""Document effective dates and supersession.

Revision ID: 0130
Revises: 0129

RAG remediation part 1 (RQ-24, FX-2.7).

1. `documents.valid_from`, `documents.valid_until` (nullable) - D5. Retrieval
   leaves out a document outside its window; NULL is open-ended.
   `validity_window_ordered` checks `valid_until > valid_from` when both are set.
2. `documents.supersedes_document_id` (nullable) with
   `fk_documents_supersedes_same_knowledge_base` on `(tenant_id,
   supersedes_document_id, knowledge_base_id)` -> `documents (tenant_id, id,
   knowledge_base_id)`, so a document can only supersede one of its own
   workspace's and knowledge base's. `ON DELETE SET NULL
   (supersedes_document_id)`: deleting the older document clears the link and
   nothing else.

**Online.** Nullable columns under a bounded `lock_timeout`; the check and the
foreign key added `NOT VALID` and validated afterwards, which takes only a
`SHARE UPDATE EXCLUSIVE` lock while existing rows are checked.

**Downgrade** refuses while any document carries a window or a supersession.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
FK = "fk_documents_supersedes_same_knowledge_base"
CHECK = "ck_documents_validity_window_ordered"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("documents", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("supersedes_document_id", sa.UUID(), nullable=True))
    op.execute(
        f"ALTER TABLE documents ADD CONSTRAINT {CHECK} CHECK"
        " (valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE documents ADD CONSTRAINT {FK}"
        " FOREIGN KEY (tenant_id, supersedes_document_id, knowledge_base_id)"
        " REFERENCES documents (tenant_id, id, knowledge_base_id)"
        " ON DELETE SET NULL (supersedes_document_id) NOT VALID"
    )
    op.execute(f"ALTER TABLE documents VALIDATE CONSTRAINT {CHECK}")
    op.execute(f"ALTER TABLE documents VALIDATE CONSTRAINT {FK}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.exec_driver_sql(
        "SELECT count(*) FROM documents WHERE valid_from IS NOT NULL"
        " OR valid_until IS NOT NULL OR supersedes_document_id IS NOT NULL"
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"Refusing to downgrade 0130: {count} documents carry effective dates or a supersession."
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {FK}")
    op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {CHECK}")
    op.drop_column("documents", "supersedes_document_id")
    op.drop_column("documents", "valid_until")
    op.drop_column("documents", "valid_from")
