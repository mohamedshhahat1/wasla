"""Give media retention somewhere to record that a file is on its way out.

Revision ID: 0038
Revises: 0037

One nullable column and one partial index. `message_media.purge_started_at` is
what makes deleting a stored file recoverable: the claim is committed before the
object is removed, so a sweep that dies mid-flight leaves a row that says what it
was doing rather than a row pointing confidently at a file that is gone
(ADR-078).

The index is partial on `storage_key IS NOT NULL`, which is the only shape the
sweep ever asks for. Once a deployment has been running a while, most rows in
this table have either been purged already or never carried a file at all, and
an index over all of them would be mostly entries no query can use.

No data change. `purge_started_at` is null on every existing row, which is
exactly what it means for a file nothing has decided to remove - so a deployment
that never sets `MEDIA_RETENTION_DAYS` is unaffected by this migration in every
respect except the two schema objects.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_media",
        sa.Column("purge_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_message_media_retention",
        "message_media",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("storage_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Reversible, unlike the enum migrations around it.

    A column and an index are both genuinely removable, and dropping them loses
    only the record of which files a sweep had claimed. A downgrade is a
    deployment going back to a release with no retention at all, where that
    record describes work nothing is going to finish.
    """
    op.drop_index("ix_message_media_retention", table_name="message_media")
    op.drop_column("message_media", "purge_started_at")
