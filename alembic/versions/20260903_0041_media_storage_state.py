"""Give an object's key a state, so a key can be committed before the object exists.

Revision ID: 0041
Revises: 0040

`message_media.storage_key` used to be written at the same instant the object
appeared, which made the pair `(storage_key, purge_started_at)` a complete
description of the file's whereabouts:

    key NULL,  purge NULL    never downloaded
    key set,   purge NULL    stored
    key set,   purge set     being purged
    key NULL,  purge set     purged

ADR-087 writes the key *first*, so that a transaction failing after a successful
object write leaves something that names the object. That adds a fifth
possibility - key set, object not yet proved to exist - which the pair above
cannot express: it looks exactly like "stored". So the state becomes a column.

## What this adds

- `storage_state`, a native enum, not null, defaulting to `absent`.
- `upload_started_at`, when the intent was committed. Reconciliation measures
  its grace period from here rather than from `updated_at`, which a later write
  to the row - a transcript arriving - would move, making a stuck upload look
  fresh for ever.
- `uq_message_media_storage_key`, so one object has at most one owning row.
- `ck_message_media_storage_state`, which is what lets every consumer trust the
  state column instead of re-deriving the lifecycle from which columns are null.
- A partial index for reconciliation's only query, and a replacement for
  retention's - now on `storage_state = 'stored'` rather than on
  `storage_key IS NOT NULL`, because those two sets are no longer the same.

## The backfill

Every existing row is classified by the pair it already carries, which is a
total mapping onto four of the six states. **No existing row can be `pending`**,
and that is a fact rather than an assumption: the state did not exist before
this migration, and a row with a key got it from a transaction that committed
after its object was written. So `key set, purge NULL` really does mean stored,
for every row already in the table.

A purged row stays purged. It maps to `purged` and never to `absent`, which is
the P2-A distinction this must not undo - a row whose file retention removed is
not a row that was never downloaded, and treating it as one would have a media
job ask Meta for a handle that expired months ago.

Nothing here touches `mime_type`. A row written before SEC-09 was closed holds
whatever type its caller claimed, and the download route still refuses to serve
a type outside `CANONICAL_TYPES` - `storage_state` says where the bytes are, not
whether they are safe to hand back, and this migration does not blur that.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

STATES = ("absent", "pending", "stored", "purging", "purged", "mismatched")

# One expression, restated here rather than imported from the model: a migration
# has to keep working when the model moves on.
STATE_INVARIANT = (
    "(storage_state = 'absent' AND storage_key IS NULL) "
    "OR (storage_state = 'pending' AND storage_key IS NOT NULL "
    "AND upload_started_at IS NOT NULL AND purge_started_at IS NULL) "
    "OR (storage_state = 'stored' AND storage_key IS NOT NULL "
    "AND purge_started_at IS NULL) "
    "OR (storage_state = 'purging' AND storage_key IS NOT NULL "
    "AND purge_started_at IS NOT NULL) "
    "OR (storage_state = 'purged' AND storage_key IS NULL "
    "AND purge_started_at IS NOT NULL) "
    "OR (storage_state = 'mismatched' AND storage_key IS NOT NULL)"
)


def upgrade() -> None:
    state = sa.Enum(*STATES, name="media_storage_state")
    state.create(op.get_bind(), checkfirst=True)

    # Added with a server default so the column can be NOT NULL from the start
    # on a table that already has rows. The default is `absent`, which is
    # correct for every row that never carried a file and is corrected below for
    # the rest.
    op.add_column(
        "message_media",
        sa.Column(
            "storage_state",
            state,
            nullable=False,
            server_default="absent",
        ),
    )
    op.add_column(
        "message_media",
        sa.Column("upload_started_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("""
        UPDATE message_media
        SET storage_state = CASE
            WHEN storage_key IS NOT NULL AND purge_started_at IS NULL THEN 'stored'
            WHEN storage_key IS NOT NULL AND purge_started_at IS NOT NULL THEN 'purging'
            WHEN storage_key IS NULL AND purge_started_at IS NOT NULL THEN 'purged'
            ELSE 'absent'
        END::media_storage_state
        """)

    op.create_unique_constraint(
        "uq_message_media_storage_key",
        "message_media",
        ["storage_key"],
    )
    # Named bare, like the model does: the metadata's `ck` convention turns it
    # into `ck_message_media_storage_state`, and spelling the prefix here would
    # double it.
    op.create_check_constraint(
        "storage_state",
        "message_media",
        STATE_INVARIANT,
    )

    # Retention's index, re-cut. The old predicate selected every row with a
    # key, which after ADR-087 includes uploads in flight - rows the sweep must
    # never look at.
    op.drop_index("ix_message_media_retention", table_name="message_media")
    op.create_index(
        "ix_message_media_retention",
        "message_media",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("storage_state = 'stored'"),
    )
    op.create_index(
        "ix_message_media_pending_upload",
        "message_media",
        ["upload_started_at"],
        unique=False,
        postgresql_where=sa.text("storage_state = 'pending'"),
    )


def downgrade() -> None:
    """Reversible, and it loses exactly one thing: which uploads were in flight.

    A row in `pending` describes an object that may or may not exist and that
    the previous release has no column to record. It goes back to what that
    release would have called it - a row with a key, which that release reads as
    stored - and the object is either there, in which case that is right, or it
    is not, in which case the row behaves as it did before this migration
    existed. Neither outcome deletes anything.

    `mismatched` is the one state a downgrade should not silently forgive, so it
    is cleared to no key at all: the previous release would otherwise serve a
    quarantined object as an ordinary attachment.
    """
    # Dropped before the update below, not after: clearing a quarantined row's
    # key is exactly what the constraint forbids while it is still in place.
    op.drop_constraint("storage_state", "message_media", type_="check")
    op.execute("""
        UPDATE message_media
        SET storage_key = NULL
        WHERE storage_state = 'mismatched'
        """)
    op.drop_index("ix_message_media_pending_upload", table_name="message_media")
    op.drop_index("ix_message_media_retention", table_name="message_media")
    op.create_index(
        "ix_message_media_retention",
        "message_media",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("storage_key IS NOT NULL"),
    )
    op.drop_constraint("uq_message_media_storage_key", "message_media", type_="unique")
    op.drop_column("message_media", "upload_started_at")
    op.drop_column("message_media", "storage_state")
    sa.Enum(name="media_storage_state").drop(op.get_bind(), checkfirst=True)
