"""record template messages as templates

Revision ID: 0006
Revises: 0005

Adds `template` to the message_kind enum and the two columns that say which
template was sent.

The enum is rebuilt rather than extended with ``ALTER TYPE ... ADD VALUE``.
That statement has no inverse - PostgreSQL cannot drop a value from an enum -
so a migration using it could not offer an honest downgrade, and CI runs
upgrade, downgrade to base, then upgrade again. Renaming the old type, creating
the new one, recasting the column through text and dropping the old type is
reversible in both directions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

WITHOUT_TEMPLATE = (
    "text",
    "image",
    "document",
    "audio",
    "video",
    "location",
    "interactive",
    "unsupported",
)
# Ordered as the Python enum declares it: `template` sits before `unsupported`,
# which stays last because it is the catch-all.
WITH_TEMPLATE = (
    "text",
    "image",
    "document",
    "audio",
    "video",
    "location",
    "interactive",
    "template",
    "unsupported",
)


def _swap_kind_enum(values: tuple[str, ...]) -> None:
    """Replace the message_kind type with one holding exactly `values`."""
    bind = op.get_bind()
    op.execute("ALTER TYPE message_kind RENAME TO message_kind_old")
    postgresql.ENUM(*values, name="message_kind", create_type=False).create(
        bind,
        checkfirst=False,
    )
    op.execute(
        "ALTER TABLE messages " "ALTER COLUMN kind TYPE message_kind USING kind::text::message_kind"
    )
    op.execute("DROP TYPE message_kind_old")


def upgrade():
    _swap_kind_enum(WITH_TEMPLATE)
    op.add_column("messages", sa.Column("template_name", sa.String(length=512), nullable=True))
    op.add_column("messages", sa.Column("template_language", sa.String(length=16), nullable=True))


def downgrade():
    op.drop_column("messages", "template_language")
    op.drop_column("messages", "template_name")
    # Templates were recorded as text before this revision, so returning them to
    # text is what the rows looked like. Done before the type is narrowed,
    # because the cast rejects any value the new type does not contain.
    op.execute("UPDATE messages SET kind = 'text' WHERE kind = 'template'")
    _swap_kind_enum(WITHOUT_TEMPLATE)
