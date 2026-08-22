"""Downloading, storing and reading what customers attach.

The shape mirrors `FollowUpService`: a service that a worker drives, holding the
decisions, with the network clients injected so every branch is testable without
a provider.

Two states carry the weight, and they are the same two follow-ups use.
`SKIPPED` means Wasla decided not to process this file - it is over the size cap,
or of a type nothing here can read - and no retry changes that. `FAILED` means
an attempt broke and another may work. Collapsing them would build a retry loop
against a wall, and would also hide the difference between "we could not" and
"we would not", which is the difference a workspace actually wants explained.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, RateLimitedError
from app.core.logging import get_logger
from app.core.storage import MediaStorage, StorageError
from app.db.models.media import MAX_TRANSCRIPT_LENGTH, MediaStatus, MessageMedia
from app.integrations.whatsapp.client import WhatsAppClient
from app.repositories.media_repository import MediaRepository

logger = get_logger(__name__)

# Types worth downloading, mapped to how they are read. Anything absent is
# skipped rather than stored: keeping bytes nothing can interpret costs disk and
# buys a file no one will ever open.
IMAGE_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"},
)
AUDIO_TYPES: Final = frozenset(
    {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/amr", "audio/aac", "audio/wav", "audio/webm"},
)
DOCUMENT_TYPES: Final = frozenset({"application/pdf", "text/plain"})

READABLE_TYPES: Final = IMAGE_TYPES | AUDIO_TYPES | DOCUMENT_TYPES


@dataclass(frozen=True, slots=True)
class MediaOutcome:
    """What one attempt at one file concluded."""

    media_id: uuid.UUID
    status: MediaStatus
    detail: str | None = None


def content_hash(data: bytes) -> str:
    """SHA-256 of the bytes that actually arrived, hex encoded.

    Computed here rather than taken from the descriptor Meta sent. The point of
    a hash is to describe what is in the store, and a value supplied by whoever
    handed over the file cannot do that.
    """
    return hashlib.sha256(data).hexdigest()


class MediaService:
    """Attached files for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        settings: Settings,
        storage: MediaStorage,
        whatsapp: WhatsAppClient | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._storage = storage
        # Optional so the parts of this service that do not touch Meta - reading
        # a stored file back, marking one skipped - work without one.
        self._whatsapp = whatsapp
        self._media = MediaRepository(session, tenant_id=tenant_id)

    async def get(self, media_id: uuid.UUID) -> MessageMedia:
        return await self._media.require_by_id(media_id)

    async def read(self, media: MessageMedia) -> bytes:
        """The stored bytes of a file that has one."""
        if media.storage_key is None:
            raise StorageError()
        return await self._storage.get(media.storage_key)

    async def download(self, media: MessageMedia) -> MediaOutcome:
        """Fetch one file from Meta and put it in the store.

        Already-stored files return without doing anything. The job that brings
        us here can be retried, and re-downloading would spend a request and a
        write to arrive at bytes we already hold.
        """
        if media.storage_key is not None:
            return MediaOutcome(media_id=media.id, status=media.status)
        if media.wa_media_id is None:
            return await self._skip(media, "This message carries no file to download.")
        if self._whatsapp is None:
            raise ExternalServiceError("WhatsApp is not configured for this worker.")

        # Asked before fetching, not after. The alternative to asking is paying
        # to move a file in order to discover it was too big to keep.
        descriptor = await self._whatsapp.probe_media(media.wa_media_id)
        mime_type = descriptor.mime_type or media.mime_type
        cap = self._settings.media_max_bytes

        if descriptor.byte_size is not None and descriptor.byte_size > cap:
            return await self._skip(
                media,
                f"This file is larger than the {cap // (1024 * 1024)} MB limit.",
            )
        if not self._is_readable(mime_type):
            return await self._skip(
                media,
                f"Files of type {mime_type or 'unknown'} cannot be read.",
            )

        media.status = MediaStatus.DOWNLOADING
        media.attempts += 1
        await self._session.flush()

        try:
            downloaded = await self._whatsapp.fetch_media(media.wa_media_id)
        except (ExternalServiceError, RateLimitedError) as error:
            # An attempt that broke, not a decision. Recorded rather than
            # raised, so the row explains itself and the worker can decide
            # whether another attempt is left.
            return await self._fail(media, str(error))

        # Checked again on what arrived. Meta's declared size is a claim, and a
        # cap enforced only against a claim is not a cap.
        if downloaded.byte_size > cap:
            return await self._skip(
                media,
                f"This file is larger than the {cap // (1024 * 1024)} MB limit.",
            )

        try:
            key = await self._storage.put(
                tenant_id=self._tenant_id,
                data=downloaded.content,
                mime_type=downloaded.mime_type or mime_type,
            )
        except StorageError as error:
            return await self._fail(media, str(error))

        media.storage_key = key
        media.mime_type = downloaded.mime_type or mime_type
        media.byte_size = downloaded.byte_size
        media.content_hash = content_hash(downloaded.content)
        media.status = MediaStatus.STORED
        media.last_error = None
        await self._session.flush()

        logger.info(
            "media.stored",
            extra={
                "tenant_id": str(self._tenant_id),
                "media_id": str(media.id),
                "byte_size": media.byte_size,
            },
        )
        return MediaOutcome(media_id=media.id, status=MediaStatus.STORED)

    async def mark_ready(self, media: MessageMedia, *, transcript: str | None) -> MediaOutcome:
        """Record what the file turned out to say.

        Truncated rather than refused if it is enormous. A forty-page PDF is a
        real thing for a customer to send, and the first several thousand
        characters of it are worth far more to an agent than a failure.
        """
        if transcript is not None:
            transcript = transcript.strip()[:MAX_TRANSCRIPT_LENGTH] or None

        media.transcript = transcript
        media.status = MediaStatus.READY
        media.last_error = None
        media.processed_at = datetime.now(UTC)
        await self._session.flush()
        return MediaOutcome(media_id=media.id, status=MediaStatus.READY)

    async def _skip(self, media: MessageMedia, reason: str) -> MediaOutcome:
        """A decision not to process, which no retry will change."""
        media.status = MediaStatus.SKIPPED
        media.last_error = reason
        media.processed_at = datetime.now(UTC)
        await self._session.flush()
        logger.info(
            "media.skipped",
            extra={"tenant_id": str(self._tenant_id), "media_id": str(media.id)},
        )
        return MediaOutcome(media_id=media.id, status=MediaStatus.SKIPPED, detail=reason)

    async def _fail(self, media: MessageMedia, reason: str) -> MediaOutcome:
        """An attempt that broke. Another may work, unless there are none left."""
        media.status = MediaStatus.FAILED
        media.last_error = reason[:500]
        media.processed_at = datetime.now(UTC)
        await self._session.flush()
        logger.warning(
            "media.failed",
            extra={
                "tenant_id": str(self._tenant_id),
                "media_id": str(media.id),
                "attempts": media.attempts,
            },
        )
        return MediaOutcome(media_id=media.id, status=MediaStatus.FAILED, detail=reason)

    def _is_readable(self, mime_type: str | None) -> bool:
        return mime_type is not None and mime_type.lower() in READABLE_TYPES
