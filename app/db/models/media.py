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
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

    `storage_key` is null until the bytes are actually somewhere. It is produced
    by the storage layer from a generated identifier and never from
    `filename`, which arrives from a stranger's phone.
    """

    __tablename__ = "message_media"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_media_message_id"),
        Index("ix_message_media_tenant_id", "tenant_id"),
        Index("ix_message_media_tenant_id_status", "tenant_id", "status"),
        Index("ix_message_media_conversation_id", "conversation_id"),
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
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
