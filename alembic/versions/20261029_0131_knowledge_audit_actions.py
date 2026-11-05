"""Audit actions for the knowledge document lifecycle.

Revision ID: 0131
Revises: 0130

RAG remediation part 1 (RQ-06, RQ-09, FX-2.4). `audit_action` gains
`knowledge_document_replaced`, `knowledge_document_metadata_updated`,
`knowledge_base_updated` and `knowledge_base_deleted`, appended in an
autocommit block as every label is.

**Online.** `ALTER TYPE ... ADD VALUE` takes no table lock.

**Downgrade** leaves the labels: PostgreSQL cannot drop an enum label, and an
unused label is harmless. It refuses while any audit row uses one, because the
code a downgrade returns to would read a value it does not know.
"""

from __future__ import annotations

from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

LABELS = (
    "knowledge_document_replaced",
    "knowledge_document_metadata_updated",
    "knowledge_base_updated",
    "knowledge_base_deleted",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.exec_driver_sql(
        "SELECT count(*) FROM audit_logs WHERE action::text IN ("  # noqa: S608
        + ", ".join(f"'{label}'" for label in LABELS)
        + ")"
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"Refusing to downgrade 0131: {used} audit rows record knowledge lifecycle actions."
        )
