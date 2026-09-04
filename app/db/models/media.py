"""What a customer attached, where the file went, and what it turned out to say.

A separate table rather than more columns on `messages`, for two reasons. Most
messages are text, and `messages` is the table every conversation read touches -
carrying a dozen mostly-null media columns through it would cost every one of
those reads. And media has a lifecycle of its own: a file is downloaded, then
read, then possibly re-read when a better model exists, none of which the
message itself participates in.

The division of labour with `Message` is deliberate:

- `Message.body` holds what the customer typed, caption included.
- `MessageMedia.transcript` holds what Wasla concluded the file says.

Those must never merge. A transcript is machine output, and a stored
conversation in which an inference is indistinguishable from the customer's own
words cannot be trusted afterwards - by a colleague reading the thread, or by
anybody asking what was actually said.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

# Kept low for the same reason follow-ups keep it low, though the cost is
# different: each attempt is a download and a provider call, so an over-eager
# retry spends real money on a file that is not going to become readable.
MAX_ATTEMPTS: Final = 3

MAX_TRANSCRIPT_LENGTH: Final = 8_000


class MediaStatus(StrEnum):
    """How far a file got.

    `SKIPPED` and `FAILED` are different states, and the distinction is the same
    one follow-ups draw. `FAILED` means an attempt broke - Meta timed out, the
    provider was down - and trying again may well work. `SKIPPED` means Wasla
    decided not to process this file: it is larger than the cap, or of a type
    nothing here can read. No retry changes either of those, and collapsing the
    two would build a retry loop against a wall.
    """

    PENDING = "pending"
    DOWNLOADING = "downloading"
    STORED = "stored"
    READY = "ready"
    SKIPPED = "skipped"
    FAILED = "failed"


MEDIA_STATUS_TYPE = _enum_type(MediaStatus, name="media_status")


class MediaStorageState(StrEnum):
    """Where the object is, which is a different question from how far the file got.

    `MediaStatus` above answers "has anybody read this yet?". This answers "do
    the bytes exist, and does Wasla own them?" - and the two have different
    owners. The media queue drives the first, with its own bounded retries; the
    upload seam drives this one, and reconciliation finishes it when a process
    dies mid-write (ADR-087).

    They were one column, in effect, and the encoding was the pair
    `(storage_key, purge_started_at)`:

        key NULL,  purge NULL    never downloaded
        key set,   purge NULL    stored
        key set,   purge set     being purged
        key NULL,  purge set     purged

    That worked while an object could only appear at the same instant its key
    was committed. It cannot survive writing the key *before* the object, which
    is what closes the orphan: "key set, purge NULL" would then mean either
    "stored" or "an object we have not yet proved is there", and every consumer
    would be guessing which.

    `MISMATCHED` is the state nothing recovers from automatically. An object is
    at our key and it is not what we wrote - different hash, different size -
    and the two safe things to do with it are both refusals: do not serve it,
    and do not delete it. Deleting would destroy the only evidence of how it got
    there.
    """

    ABSENT = "absent"
    PENDING = "pending"
    STORED = "stored"
    PURGING = "purging"
    PURGED = "purged"
    MISMATCHED = "mismatched"


MEDIA_STORAGE_STATE_TYPE = _enum_type(MediaStorageState, name="media_storage_state")

# The storage states in which a workspace is still holding bytes, and is
# therefore still occupying its storage capacity.
#
# Defined by what is *not* in it, which is the safer direction: `ABSENT` names
# no object, and `PURGED` has had its object deleted and its key cleared.
# Everything else does name one.
#
# `PENDING` counts, and that is what makes the intent a reservation: the row
# names an object that is about to exist, committed before the write (ADR-087),
# so a second upload asking "is there room" sees the first one's claim.
# `MISMATCHED` counts because the object is still in the bucket and is
# deliberately never deleted. `PURGING` counts because a delete in flight is
# not a delete that happened - handing out its capacity early would let a
# workspace exceed the cap whenever a retention sweep is running.
OCCUPYING_STORAGE_STATES: Final = frozenset(
    {
        MediaStorageState.PENDING,
        MediaStorageState.STORED,
        MediaStorageState.PURGING,
        MediaStorageState.MISMATCHED,
    }
)

# The statuses that still owe the conversation an answer. A conversation with
# any media in one of these is not ready for the agent to reply to, which is
# what the worker checks before it enqueues.
UNRESOLVED_MEDIA_STATUSES: Final = frozenset(
    {MediaStatus.PENDING, MediaStatus.DOWNLOADING, MediaStatus.STORED}
)


class MessageMedia(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One file attached to one message.

    One row per message, enforced: WhatsApp sends a single attachment per
    message, and a webhook replay must not add a second. That constraint is also
    what makes the download job idempotent - a retry finds the existing row
    rather than queueing another copy of the same file.

    `storage_key` is the object this row owns, and it is committed **before**
    the object is written - that is the whole of ADR-087. So a key present does
    not mean bytes present; `storage_state` says which. The key is produced from
    a generated identifier and never from `filename`, which arrives from a
    stranger's phone.
    """

    __tablename__ = "message_media"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_media_message_id"),
        # One media row owns one object key, asserted by the database rather
        # than by the odds. A generated UUID makes a collision impossible in
        # practice, but the property that matters here is not collision: it is
        # that a *second* row cannot come to reference an object the first one
        # is responsible for, because reconciliation would then have two owners
        # for one object and no way to choose (ADR-087). NULL repeats freely,
        # which is what every row that never had a file, and every purged row,
        # needs.
        UniqueConstraint("storage_key", name="uq_message_media_storage_key"),
        Index("ix_message_media_tenant_id", "tenant_id"),
        Index("ix_message_media_tenant_id_status", "tenant_id", "status"),
        Index("ix_message_media_conversation_id", "conversation_id"),
        # The retention sweep's only query: fully stored files, oldest first.
        # Partial, because the rows it must never look at - every file already
        # purged, every message that carried none, and now every upload still
        # in flight - are the overwhelming majority once a deployment has been
        # running a while (ADR-078).
        Index(
            "ix_message_media_retention",
            "created_at",
            postgresql_where=text("storage_state = 'stored'"),
        ),
        # Reconciliation's only query: intents whose upload never finished,
        # oldest first. Partial for the same reason and more sharply - a
        # healthy deployment has none of these at all (ADR-087).
        Index(
            "ix_message_media_pending_upload",
            "upload_started_at",
            postgresql_where=text("storage_state = 'pending'"),
        ),
        # What each state is allowed to look like. These are not belt and
        # braces over the services: they are the reason a consumer can trust
        # `storage_state` alone and never re-derive the lifecycle from which
        # columns happen to be null.
        CheckConstraint(
            "(storage_state = 'absent' AND storage_key IS NULL) "
            "OR (storage_state = 'pending' AND storage_key IS NOT NULL "
            "AND upload_started_at IS NOT NULL AND purge_started_at IS NULL) "
            "OR (storage_state = 'stored' AND storage_key IS NOT NULL "
            "AND purge_started_at IS NULL) "
            "OR (storage_state = 'purging' AND storage_key IS NOT NULL "
            "AND purge_started_at IS NOT NULL) "
            "OR (storage_state = 'purged' AND storage_key IS NULL "
            "AND purge_started_at IS NOT NULL) "
            "OR (storage_state = 'mismatched' AND storage_key IS NOT NULL)",
            # Bare, because the metadata's `ck` convention interpolates it into
            # `ck_%(table_name)s_%(constraint_name)s`. Spelling the prefix here
            # too would produce `ck_message_media_ck_message_media_...`.
            name="storage_state",
        ),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalised from the message. The worker asks "is anything still pending
    # on this conversation?" before it lets an agent answer, and that question
    # must be answerable without joining through the message table.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Meta's handle for the file. Nullable because an outbound attachment has no
    # inbound handle until it is uploaded.
    wa_media_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[MediaStatus] = mapped_column(
        MEDIA_STATUS_TYPE,
        nullable=False,
        default=MediaStatus.PENDING,
    )
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # As the customer's phone reported it. Shown to people; never used to build
    # a path.
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # SHA-256 of the bytes that actually arrived, hex encoded - computed here
    # rather than taken from the descriptor Meta sent.
    #
    # Written *before* the object, alongside the key, and it is what makes an
    # interrupted upload recoverable: reconciliation reads the object back and
    # compares this, so an object at our key that is not what we meant to write
    # is refused rather than adopted (ADR-087). Once the upload is finalised the
    # same value describes what is in the store, so there is one hash column
    # rather than an expectation and a truth that always agree.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Which of the six states above this row is in. The column consumers read;
    # `storage_key IS NULL` answers a different question and answers it wrongly
    # for two of them.
    storage_state: Mapped[MediaStorageState] = mapped_column(
        MEDIA_STORAGE_STATE_TYPE,
        nullable=False,
        default=MediaStorageState.ABSENT,
        server_default=MediaStorageState.ABSENT.value,
    )
    # When the intent to write this object was committed. Reconciliation
    # measures its grace period from here rather than from `updated_at`, which
    # any later write to the row would move - and a transcript arriving would
    # then make a stuck upload look fresh for ever.
    upload_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A recorded voice note, as opposed to an attached audio file. Both are
    # transcribed; only one is somebody speaking to the business.
    is_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # What the file says: a transcript for audio, a description for an image,
    # extracted text for a document. Never the customer's own words.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the retention sweep decided this file should go (ADR-078).
    #
    # The timestamp, no longer the state: `storage_state` carries `PURGING` and
    # `PURGED` now, and this says *when* the claim was made. The mechanism it
    # exists for is unchanged. Deleting an object and clearing the column that
    # points at it are two writes to two systems and cannot be one transaction,
    # so the claim is committed *first*: a sweep that dies after removing the
    # object leaves a row that says so, the next pass deletes again (which is a
    # no-op) and finishes the job. The alternative order - delete then record -
    # leaves a row pointing confidently at a file that is gone, and nothing
    # anywhere to distinguish that from a broken store.
    purge_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_resolved(self) -> bool:
        """Whether this file no longer holds up a reply.

        `SKIPPED` and `FAILED` count as resolved. The customer is still owed an
        answer even when the attachment could not be read, and an agent that
        says so is better than one that never speaks.
        """
        return self.status not in UNRESOLVED_MEDIA_STATUSES

    @property
    def is_exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS

    @property
    def is_purged(self) -> bool:
        """Whether the file is gone, as opposed to merely unreadable.

        A colleague opening an attachment that retention removed should be told
        that it was removed, which is a different sentence from the store being
        unavailable - and only this row can tell them apart.
        """
        return self.storage_state is MediaStorageState.PURGED

    @property
    def is_stored(self) -> bool:
        """Whether there are bytes at this row's key that Wasla has verified.

        The one question every consumer of a file must ask, and the reason the
        state column exists. A key is not a file: between the intent commit and
        the finalisation there is a row carrying a perfectly well-formed
        `storage_key` and an object that may not be there at all, and serving
        from it would answer a colleague with a storage error for something the
        system is in the middle of doing correctly.
        """
        return self.storage_state is MediaStorageState.STORED
