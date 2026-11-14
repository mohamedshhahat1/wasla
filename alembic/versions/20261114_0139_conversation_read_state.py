"""The team's read position on a conversation: what has been seen, by whom, when.

Revision ID: 0139
Revises: 0138

FE-4, UR-02 (ADR-198). Three nullable columns on `conversations`:

- `last_read_message_id` - the newest message the team has seen. A marker,
  not a reference: no foreign key, because a key from `conversations` into
  `messages` would make every message delete look the marker up, and a
  workspace purge deletes millions. A message only goes with its conversation
  (cascade), and a marker naming no message of its conversation resolves to no
  position, which reads as "nothing seen".
- `last_read_by_user_id` - who moved it last; `users` with `SET NULL`, the rule
  `assigned_to_id` already follows.
- `last_read_at` - when.

NULL is "nothing has been read". No backfill: every existing conversation
starts unread, which is what nobody having opened it in the inbox means.

**Online.** Three nullable columns without a default: a catalog change only.

**Downgrade** drops the three columns and nothing else.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "conversations",
        sa.Column("last_read_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_read_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_conversations_last_read_by_user_id_users"),
        "conversations",
        "users",
        ["last_read_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_constraint(
        op.f("fk_conversations_last_read_by_user_id_users"), "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "last_read_at")
    op.drop_column("conversations", "last_read_by_user_id")
    op.drop_column("conversations", "last_read_message_id")
