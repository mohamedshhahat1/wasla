"""add colleague media access to the audit vocabulary

Revision ID: 0067
Revises: 0066

Two labels: ``media_downloaded`` and ``media_sent``.

A colleague opening a customer's photograph, voice note or document, and a
colleague sending a customer a file, left no audit entry (MEDIA-17). The message
row recorded who sent an attachment; nothing recorded who looked at one. Both
are now audited with internal identifiers only (PD-MEDIA-06).

Additive, like every enum migration in this history, and it must be applied
before the code that writes the labels: a native PostgreSQL enum refuses a label
it does not carry, and the download that tried to audit would fail with it.
"""

from __future__ import annotations

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

NEW_ACTIONS = (
    "media_downloaded",
    "media_sent",
)


def upgrade() -> None:
    # `ADD VALUE` cannot run inside the transaction Alembic opens, and
    # `IF NOT EXISTS` keeps the step re-runnable after a partial failure.
    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Nothing to undo, for the reasons migration 0051 gives.

    PostgreSQL cannot drop an enum label, and rewriting `audit_logs` onto a new
    type to remove two inert labels would be a full rewrite of the one table
    that only ever grows. Roll the code back first and leave the labels.
    """
