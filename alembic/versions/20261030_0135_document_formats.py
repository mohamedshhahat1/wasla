"""Catalogues and office documents as knowledge sources.

Revision ID: 0135
Revises: 0134

RAG remediation part 3 (RQ-21, FX-3.17, FX-4.5, D9).

`document_source` gains `csv`, `xlsx`, `docx` and `html`, each appended in an
autocommit block as every label is (`ADD VALUE` cannot run inside a transaction
block that then uses it, and takes no table lock).

**Downgrade** refuses while any document or generation is of one of the new
formats: the code a downgrade returns to cannot read them, and would fail every
re-index of a catalogue the workspace uploaded. Otherwise it changes nothing -
PostgreSQL cannot drop an enum label, so the labels stay, unused.
"""

from __future__ import annotations

from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None

LABELS = ("csv", "xlsx", "docx", "html")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE document_source ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    bind = op.get_bind()
    listed = ", ".join(f"'{label}'" for label in LABELS)
    documents = bind.exec_driver_sql(
        f"SELECT count(*) FROM documents WHERE source::text IN ({listed})"  # noqa: S608
    ).scalar_one()
    generations = bind.exec_driver_sql(
        f"SELECT count(*) FROM document_index_generations WHERE source::text IN ({listed})"  # noqa: S608
    ).scalar_one()
    if documents or generations:
        raise RuntimeError(
            "Refusing to downgrade 0135: it would strand "
            f"{documents} documents and {generations} generations of CSV, Excel, Word or HTML "
            "that the earlier code cannot read."
        )
