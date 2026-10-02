"""Automation disclosure: when it was last said, when the AI took over again, and the wording.

Revision ID: 0086
Revises: 0085

OMNI-041 (ADR-127). Messenger's and Instagram's policy require an automated chat
experience to disclose that it is automated - at the start, after significant
lapses, and when a conversation moves from a person back to automation.

1. **`conversations.automation_disclosed_at`** - when an AI reply that disclosed
   was delivered.
2. **`conversations.ai_resumed_at`** - when a colleague last handed the
   conversation back to the AI.
3. **`tenants.automation_disclosure`** - a workspace's own wording, by language.

**Online.** Nullable, no default: metadata-only `ADD COLUMN`s under a bounded
`lock_timeout`, no rewrite of `conversations`.

**Downgrade refuses** while any row holds a disclosure, a hand-back or a
workspace's wording: dropping them would forget what a customer was told.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

DOWNGRADE_PRECHECKS: tuple[tuple[str, str], ...] = (
    (
        "conversations with a recorded disclosure or hand-back",
        "SELECT count(*) FROM conversations"
        " WHERE automation_disclosed_at IS NOT NULL OR ai_resumed_at IS NOT NULL",
    ),
    (
        "workspaces with their own disclosure wording",
        "SELECT count(*) FROM tenants WHERE automation_disclosure IS NOT NULL",
    ),
)


def _refuse(checks: tuple[tuple[str, str], ...], preamble: str) -> None:
    connection = op.get_bind()
    found = []
    for label, query in checks:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"{preamble} (docs/RUNBOOK.md, 'Omnichannel final remediation (0085-0091)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "conversations",
        sa.Column("automation_disclosed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("ai_resumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("automation_disclosure", postgresql.JSONB(), nullable=True),
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse(
        DOWNGRADE_PRECHECKS,
        "Rows hold disclosure records the pre-0086 schema cannot represent",
    )
    op.drop_column("tenants", "automation_disclosure")
    op.drop_column("conversations", "ai_resumed_at")
    op.drop_column("conversations", "automation_disclosed_at")
    op.execute("RESET lock_timeout")
