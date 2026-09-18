"""record which attempt holds an attached file, and index the unresolved ones

Revision ID: 0065
Revises: 0064

Two columns and one partial index on ``message_media``.

``claim_id`` and ``claimed_at`` say which worker attempt is processing a file
right now, and since when. The media worker used to hold a row lock and a
transaction across the Meta download so a duplicate job would wait behind it
(MEDIA-11); the lock is gone - no transaction spans a network call now - and a
committed claim takes its place. A second attempt at the same file finds a live
claim and stands aside, so a duplicate job still costs no second paid read, and
every write the claimant makes afterwards checks the claim is still its own.

``claimed_at`` is also how a stranded file is recognised. A row whose job
dead-lettered, or whose worker died holding it, used to stay ``pending`` or
``downloading`` for ever, and because a conversation's reply waits for every
attachment on it to resolve, every later attachment in that conversation went
unanswered too (MEDIA-03, MEDIA-04). A claim older than the longest an attempt
can take is a claim nobody is honouring, and the media recovery sweep finishes
it.

``ix_message_media_unresolved`` serves that sweep and the stranded-media gauge
read at every scrape. It holds only rows still owing the conversation an answer,
which on a healthy deployment is the handful in flight.

Both columns are nullable and nothing is backfilled: an existing unresolved row
has no claim, which is exactly what it is, and the sweep measures it from its
creation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_media",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "message_media",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_message_media_unresolved",
        "message_media",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending', 'downloading', 'stored')"),
    )


def downgrade() -> None:
    op.drop_index("ix_message_media_unresolved", table_name="message_media")
    op.drop_column("message_media", "claimed_at")
    op.drop_column("message_media", "claim_id")
