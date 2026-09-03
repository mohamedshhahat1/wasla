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
from app.core.media_types import SNIFF_BYTES, DetectedMedia, MediaTypeError
from app.core.media_types import resolve as resolve_media_type
from app.core.storage import MediaStorage, StorageError, build_key
from app.db.models.media import (
    MAX_TRANSCRIPT_LENGTH,
    MediaStatus,
    MediaStorageState,
    MessageMedia,
)
from app.db.models.usage import UsageEventType
from app.db.session import released
from app.integrations.whatsapp.client import MediaTooLargeError, WhatsAppClient
from app.repositories.media_repository import MediaRepository
from app.services.extraction import UnreadableDocumentError
from app.services.media_reader import (
    READABLE_TYPES,
    TRANSCRIPTION_METHOD,
    MediaReader,
    ScannedDocumentError,
    SilentRecordingError,
)
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# Storage states this path must leave alone. Each is owned by something else -
# retention, or an operator looking at a quarantined object - and restarting a
# download from one of them would be this service overruling that owner.
_NOT_OURS_TO_WRITE: Final = frozenset({MediaStorageState.PURGING, MediaStorageState.MISMATCHED})


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
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    async def get(self, media_id: uuid.UUID) -> MessageMedia:
        return await self._media.require_by_id(media_id)

    async def read(self, media: MessageMedia) -> bytes:
        """The stored bytes of a file that has one.

        `is_stored`, not `storage_key is not None`. A row carrying a key is a
        row that has *claimed* one, and between the intent commit and the
        finalisation there may be nothing at it yet - reading from there would
        answer a colleague with a storage error for a file the system is in the
        middle of writing correctly (ADR-087).
        """
        if not media.is_stored or media.storage_key is None:
            raise StorageError()
        return await self._storage.get(media.storage_key)

    async def download(self, media: MessageMedia) -> MediaOutcome:
        """Fetch one file from Meta and put it in the store.

        Three phases, and the boundaries between them are the point (ADR-087):

            TX1   allocate the object key, record what is about to be written,
                  state PENDING                                    -> COMMIT
            --    write the object. No transaction, no connection held.
            TX2   re-read under a row lock, confirm it is still ours,
                  state STORED                                     -> COMMIT

        The commit in the middle is what makes an interrupted write
        recoverable. Anything that goes wrong after it leaves a row that names
        the exact object, so reconciliation can find it without knowing
        anything about the bucket's contents.

        Already-stored files return without doing anything. The job that brings
        us here can be retried, and re-downloading would spend a request and a
        write to arrive at bytes we already hold.
        """
        if media.is_stored:
            return MediaOutcome(media_id=media.id, status=media.status)
        if media.storage_state in _NOT_OURS_TO_WRITE:
            # Being purged, already quarantined: each has an owner, and it is
            # not this. Reported as it stands rather than restarted.
            return MediaOutcome(media_id=media.id, status=media.status)
        if media.is_purged:
            # Retention removed this file (ADR-078). A null `storage_key` used
            # to mean one thing - not downloaded yet - and now means two, so
            # this is checked before anything treats the row as fresh work.
            #
            # Without it a media job replayed after a purge would ask Meta for
            # a handle that expired months ago, fail, and flip a READY row with
            # a good transcript to FAILED - undoing the deletion the workspace
            # asked for if it happened to succeed, and destroying the record of
            # what the file said if it did not.
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
            downloaded = await self._whatsapp.fetch_media(media.wa_media_id, max_bytes=cap)
        except MediaTooLargeError:
            # Caught before the clause below, which would call this a failure
            # and retry it. Meta's declared size is a claim, and this is the
            # branch where the claim turned out to be wrong - the read was
            # abandoned mid-body rather than completed and then measured, so
            # the worker never held more than the cap.
            return await self._skip(
                media,
                f"This file is larger than the {cap // (1024 * 1024)} MB limit.",
            )
        except (ExternalServiceError, RateLimitedError) as error:
            # An attempt that broke, not a decision. Recorded rather than
            # raised, so the row explains itself and the worker can decide
            # whether another attempt is left.
            return await self._fail(media, str(error))

        # The second trust boundary SEC-09 named. Everything above this line
        # decided what to do from `mime_type`, which is a string in Meta's
        # media descriptor - and a descriptor is a claim about a file, not the
        # file. From here the bytes decide: a file whose contents contradict
        # what Meta said it was, or that is not a supported format at all, is
        # skipped rather than stored and read as the thing it claimed to be.
        #
        # `SKIPPED` and not `FAILED`: no number of retries turns these bytes
        # into the type they were announced as, and a retry loop against that
        # is exactly what the two states exist to keep apart.
        try:
            detected = resolve_media_type(
                claimed=downloaded.mime_type or mime_type,
                prefix=downloaded.content[:SNIFF_BYTES],
            )
        except MediaTypeError:
            logger.warning(
                "media.type_mismatch",
                extra={
                    "event": "media.type_mismatch",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(media.id),
                },
            )
            return await self._skip(
                media,
                "This file's contents do not match the type it arrived as, "
                "so it was not stored.",
            )

        # TX1. The object does not exist yet and the database already knows its
        # name, its size and the hash of what belongs in it.
        key = await self.intend(
            media,
            mime_type=detected.mime_type,
            data=downloaded.content,
        )
        if key is None:
            return await self._fail(
                media,
                "This file no longer matches the upload already recorded for it.",
            )

        # No transaction, no pooled connection, no row lock across the write.
        # `released` commits what is staged, which is exactly the intent above
        # and is why it is safe here (ADR-080).
        async with released(self._session):
            written = await self._write(key=key, data=downloaded.content, mime_type=detected)

        if not written:
            # The intent stays. A row that names an object nobody managed to
            # write is the recoverable state, and reconciliation decides
            # afterwards whether it is there.
            return await self._fail(media, "The file store refused the write.")

        # TX2.
        return await self.finalize(media, key=key)

    async def intend(
        self,
        media: MessageMedia,
        *,
        mime_type: str,
        data: bytes,
    ) -> str | None:
        """Record which object is about to be written, and what will be in it.

        Returns the key to write at, or None if this file must not be written -
        which happens when a row already carries an intent describing different
        bytes. Overwriting there would replace an object somebody may already
        be recovering with contents its own row does not describe.

        Allocated under a row lock, so two attempts at one file agree on one
        key rather than writing two objects of which only one can be recorded
        (ADR-087). The lock covers a `SELECT` and an `UPDATE` and nothing else;
        the caller commits immediately afterwards.
        """
        digest = content_hash(data)
        byte_size = len(data)
        row = await self._media.lock_for_upload(media.id)

        if row.storage_state is MediaStorageState.PENDING:
            # An earlier attempt got this far and did not finish. Reuse its
            # key, so a retry cannot leak the object the first one wrote.
            if row.content_hash != digest or row.byte_size != byte_size:
                logger.warning(
                    "media.upload_intent_conflict",
                    extra={
                        "event": "media.upload_intent_conflict",
                        "tenant_id": str(self._tenant_id),
                        "media_id": str(media.id),
                    },
                )
                return None
            return row.storage_key
        if row.storage_state is not MediaStorageState.ABSENT:
            return None

        row.storage_key = build_key(tenant_id=self._tenant_id, mime_type=mime_type)
        row.storage_state = MediaStorageState.PENDING
        row.upload_started_at = datetime.now(UTC)
        # The canonical type, so everything downstream - the reader that picks
        # a route, the download handler that sets a Content-Type - works from
        # what the file is rather than from what it was announced as.
        row.mime_type = mime_type
        row.byte_size = byte_size
        row.content_hash = digest
        await self._session.flush()

        logger.info(
            "media.upload_intent_created",
            extra={
                "event": "media.upload_intent_created",
                "tenant_id": str(self._tenant_id),
                "media_id": str(media.id),
                "byte_size": byte_size,
            },
        )
        return row.storage_key

    async def finalize(self, media: MessageMedia, *, key: str) -> MediaOutcome:
        """Record that the object this row claimed is now really there.

        Re-read under the same lock the intent took, and refused if the row has
        moved on: a duplicate attempt that got here first, or a reconciler that
        decided while this one was writing. Finalising anyway would meter the
        same bytes twice and could resurrect a state somebody else settled.
        """
        row = await self._media.lock_for_upload(media.id)
        if row.storage_state is not MediaStorageState.PENDING or row.storage_key != key:
            return MediaOutcome(media_id=row.id, status=row.status)

        row.storage_state = MediaStorageState.STORED
        row.status = MediaStatus.STORED
        row.last_error = None
        # Storage is metered when bytes are written, not by sweeping the store.
        # A sweep would report a level rather than a consumption, and a level
        # cannot be billed for a period that has already closed. Metered by
        # whoever finalises - this path or reconciliation - because the
        # transition happens exactly once, under this lock.
        self._usage.record(
            UsageEventType.STORAGE_USED,
            quantity=row.byte_size,
            meta={"media_id": str(row.id)},
        )
        await self._session.flush()

        logger.info(
            "media.stored",
            extra={
                "event": "media.upload_finalized",
                "tenant_id": str(self._tenant_id),
                "media_id": str(row.id),
                "byte_size": row.byte_size,
            },
        )
        return MediaOutcome(media_id=row.id, status=MediaStatus.STORED)

    async def _write(self, *, key: str, data: bytes, mime_type: DetectedMedia) -> bool:
        """Put the bytes at the key the intent named. Never raises.

        A refusal is a `False` rather than an exception because the caller is
        inside `released`, where nothing may touch the session - and the row
        that has to record the failure is on the other side of that block.
        """
        try:
            await self._storage.put_at(key=key, data=data, mime_type=mime_type.mime_type)
        except StorageError:
            return False
        return True

    async def understand(self, media: MessageMedia, *, reader: MediaReader) -> MediaOutcome:
        """Work out what a stored file says, and record it.

        The three outcomes are deliberately distinct, and which one a failure
        gets is decided here rather than in the reader - this is the only place
        that knows how many attempts are left.

        - Read: `READY`, with the transcript.
        - Nothing to read - a silent recording, a scanned page, an unsupported
          type: `SKIPPED`. The file was opened and there was no text in it, and
          no number of retries changes that.
        - The attempt broke: `FAILED`, and retryable until the attempts run out,
          at which point it becomes a give-up that still lets the customer be
          answered.
        """
        if media.is_purged:
            # Read already, and the file since removed. The transcript on the
            # row is the answer, and re-deriving it is not possible anyway.
            return MediaOutcome(media_id=media.id, status=media.status)
        if not media.is_stored or media.storage_key is None:
            # A row mid-upload has a key and no proven object. Reading from it
            # would be this job consuming a write that has not been finalised,
            # and calling the resulting storage error "nothing to read" would
            # skip a file that is about to be perfectly readable (ADR-087).
            return await self._skip(media, "There is nothing stored to read.")
        if media.status is MediaStatus.READY:
            # Already read. The job can be retried, and paying a provider again
            # to arrive at the transcript already on the row would be waste.
            return MediaOutcome(media_id=media.id, status=MediaStatus.READY)

        try:
            content = await self._storage.get(media.storage_key)
        except StorageError as error:
            return await self._fail(media, str(error))

        try:
            result = await reader.read(content=content, mime_type=media.mime_type)
        except (SilentRecordingError, ScannedDocumentError, UnreadableDocumentError) as decision:
            # Not failures. The file was opened and found to hold no text, which
            # is an answer rather than an error.
            return await self._skip(media, decision.message)
        except (ExternalServiceError, RateLimitedError) as error:
            if media.is_exhausted:
                return await self._skip(
                    media,
                    "This file could not be read after several attempts.",
                )
            return await self._fail(media, str(error))

        # One file read, whatever it took to read it. Transcription is metered
        # separately because it is a second provider and priced as one - but as
        # a count of recordings, not their length: the configured models report
        # no duration, and inferring seconds from a compressed byte count would
        # put a fabricated number in a bill.
        self._usage.record(
            UsageEventType.MEDIA_PROCESSING,
            meta={"media_id": str(media.id), "method": result.method},
        )
        if result.method == TRANSCRIPTION_METHOD:
            self._usage.record(
                UsageEventType.VOICE_TRANSCRIPTION,
                meta={"media_id": str(media.id), "byte_size": media.byte_size},
            )

        logger.info(
            "media.read",
            extra={
                "tenant_id": str(self._tenant_id),
                "media_id": str(media.id),
                "method": result.method,
            },
        )
        return await self.mark_ready(media, transcript=result.transcript)

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
