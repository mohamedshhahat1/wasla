"""Downloading, storing and reading what customers attach.

The shape mirrors `FollowUpService`: a service that a worker drives, holding the
decisions, with the network clients injected so every branch is testable without
a provider.

Two terminal states carry the weight, and they are the same two follow-ups use.
`SKIPPED` means Wasla decided not to process this file - it is over the size cap,
of a type nothing here can read, or its workspace is no longer served. `FAILED`
means an attempt broke. Both are final: a provider failure is retried inside the
provider's client, a bounded handful of times, and then the file is given up on
so the customer can be answered (PD-MEDIA-08). What the two states still keep
apart is "we could not" from "we would not", which is the difference a workspace
actually wants explained.

## One attempt, and what it holds

Every attempt at a file runs in the same shape, and the shape is the fix for
three findings at once:

    TX    re-read the row under a lock, take the claim, attempts + 1  -> COMMIT
    --    Meta: descriptor, redirects, body - one wall-clock deadline
          (no transaction, no pooled connection, no row lock)
    TX    check the claim is still ours; decide; record the intent    -> COMMIT
    --    write the object (no transaction)
    TX    check the claim; finalise                                   -> COMMIT
    --    read the object back and understand it - one deadline
          (no transaction)
    TX    check the claim; record the result; clear the claim

**No database connection is held across somebody else's network** (MEDIA-11).
The download used to flush `DOWNLOADING` and then fetch inside the same
transaction, holding a pooled connection and the row lock for as long as Meta,
a CDN or a slow host cared to take.

**The claim replaces the lock.** A duplicate job for the same file finds a live
claim and stands aside, so it costs no second paid read; and every write the
claimant makes afterwards first checks the claim is still its own, so an
attempt that outlived its claim - because the recovery sweep decided it was
dead - can never overwrite what came after it.

**Every exit is terminal or owned.** A failure anywhere on the network - the
descriptor lookup included, which used to sit outside any `try` (MEDIA-04) - is
classified into a closed reason and written as `SKIPPED` or `FAILED`. What the
attempt cannot write, because its job died or its worker did, the claim makes
findable: `MediaRecoveryService` finishes it (MEDIA-03).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialDecryptionError
from app.core.exceptions import (
    ExternalServiceError,
    PlanLimitExceededError,
    RateLimitedError,
)
from app.core.logging import get_logger
from app.core.media_types import SNIFF_BYTES, MediaTypeError
from app.core.media_types import resolve as resolve_media_type
from app.core.storage import MediaStorage, StorageError, build_key
from app.db.models.billing import LimitKey
from app.db.models.media import (
    MAX_TRANSCRIPT_LENGTH,
    MediaStatus,
    MediaStorageState,
    MessageMedia,
)
from app.db.models.usage import UsageEventType
from app.db.session import released
from app.integrations.whatsapp.client import (
    DownloadedMedia,
    MalformedMediaDescriptorError,
    MediaCredentialRefusedError,
    MediaHostRefusedError,
    MediaTooLargeError,
    MediaUnavailableError,
    WhatsAppClient,
)
from app.repositories.media_repository import MediaRepository
from app.services.credential_service import CredentialService
from app.services.entitlement_service import EntitlementService
from app.services.extraction import UnreadableDocumentError
from app.services.media_horizons import claim_lease
from app.services.media_outcomes import (
    MAX_REASON_LENGTH,
    MediaReason,
    status_for,
    text_for,
)
from app.services.media_reader import (
    READABLE_TYPES,
    TRANSCRIPTION_METHOD,
    DocumentBeyondLimitsError,
    MediaReader,
    ReadResult,
    ScannedDocumentError,
    SilentRecordingError,
)
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# Storage states this path must leave alone. Each is owned by something else -
# retention, or an operator looking at a quarantined object - and restarting a
# download from one of them would be this service overruling that owner.
_NOT_OURS_TO_WRITE: Final = frozenset({MediaStorageState.PURGING, MediaStorageState.MISMATCHED})

# The statuses a row may finish in. A row already in one of them is left as it
# stands by every entry point here: the file has been answered for.
TERMINAL_STATUSES: Final = frozenset({MediaStatus.READY, MediaStatus.SKIPPED, MediaStatus.FAILED})

# What a row says when its reader raised something unexpected. Wasla's words,
# never the exception's: an agent is shown this line, and an exception raised
# while opening a customer's file can quote the file.
READER_FAILED: Final = text_for(MediaReason.READER_FAILED)

# The decisions a reader can express, each with its own fixed sentence. Not
# failures: the file was opened and there was nothing to read in it, or more
# than the bounded parser will read - an answer rather than an error.
_READER_DECISIONS: Final = (
    SilentRecordingError,
    ScannedDocumentError,
    UnreadableDocumentError,
    DocumentBeyondLimitsError,
)


@dataclass(frozen=True, slots=True)
class MediaOutcome:
    """What one attempt at one file concluded.

    `deferred` is the outcome that is not a conclusion: another attempt holds
    the file, and this one stood aside without writing anything.
    """

    media_id: uuid.UUID
    status: MediaStatus
    detail: str | None = None
    reason: MediaReason | None = None
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class _Fetched:
    """What the network half of a download produced: bytes, or a reason not to."""

    downloaded: DownloadedMedia | None = None
    refusal: MediaReason | None = None
    declared_type: str | None = None


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
        whatsapp_for: Callable[[str], WhatsAppClient] | None = None,
        credentials: CredentialService | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._storage = storage
        # Optional so the parts of this service that do not touch Meta - reading
        # a stored file back, marking one skipped - work without one.
        self._whatsapp = whatsapp
        # How a production attempt gets its client: from the credential of the
        # number the file arrived on, resolved when the file is claimed
        # (MEDIA-13). `whatsapp` above is the pre-built alternative tests use.
        self._whatsapp_for = whatsapp_for
        self._credentials = credentials or CredentialService(settings)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)
        self._entitlements = EntitlementService(
            session,
            tenant_id=tenant_id,
            default_plan_code=settings.default_plan_code,
        )
        # The claim this service instance holds, if it has taken one. One
        # instance is one attempt, which is what makes this a fencing token.
        self._claim_id: uuid.UUID | None = None

    @property
    def claim_id(self) -> uuid.UUID | None:
        return self._claim_id

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

    # ---------------------------------------------------------------- one attempt

    async def process(self, media: MessageMedia, *, reader: MediaReader) -> MediaOutcome:
        """Take one file as far as it will go: stored and read, or terminal.

        What the worker calls. Downloading and understanding stay separate
        methods because each is also a unit on its own - a retried job whose
        file was already stored only needs the second - but this is the one
        place that decides whether the second follows the first.
        """
        outcome = await self.download(media)
        if outcome.deferred or outcome.status in TERMINAL_STATUSES:
            return outcome
        if not media.is_stored:
            # Neither stored nor terminal: somebody else owns the row's
            # storage state right now (retention, quarantine). Reported as it
            # stands.
            return outcome
        return await self.understand(media, reader=reader)

    async def download(self, media: MessageMedia) -> MediaOutcome:
        """Fetch one file from Meta and put it in the store.

        The write protocol is ADR-087's, unchanged: the intent - key, size,
        hash - commits before the object exists, the object is written with no
        transaction open, and finalisation re-reads under a lock. What is new
        is everything around it: the claim, the deadline, and the rule that
        no transaction is open while Meta is being asked for anything.

        Already-stored files return without doing anything. The job that brings
        us here can be retried, and re-downloading would spend a request and a
        write to arrive at bytes we already hold.
        """
        row = await self._media.lock_for_upload(media.id)
        if row.status in TERMINAL_STATUSES:
            return self._as_it_stands(row)
        if row.is_stored:
            self._settle_stored(row)
            return MediaOutcome(media_id=row.id, status=MediaStatus.STORED)
        if row.storage_state in _NOT_OURS_TO_WRITE or row.is_purged:
            # Being purged, already quarantined, or removed by retention
            # (ADR-078): each has an owner, and it is not this. A null key used
            # to mean one thing - not downloaded yet - and now means several, so
            # this is checked before anything treats the row as fresh work.
            return self._as_it_stands(row)
        if row.wa_media_id is None:
            return await self._finish(row, MediaReason.NO_FILE)
        refusal = await self._lifecycle_refusal(row)
        if refusal is not None:
            # Before Meta is asked anything: no descriptor, no download, no
            # object, no provider spend for a workspace the platform has
            # stopped serving (MEDIA-08, PD-MEDIA-05).
            return await self._finish(row, refusal)
        whatsapp = self._whatsapp
        if whatsapp is None:
            whatsapp = await self._client_for(row)
        if whatsapp is None:
            # No credential to fetch with. Terminal and observable rather than
            # a job dead-lettered with the row left pending (MEDIA-03).
            return await self._finish(row, MediaReason.CREDENTIAL_UNAVAILABLE)

        claimed = await self._claim(row, status=MediaStatus.DOWNLOADING)
        if claimed is not None:
            return claimed

        wa_media_id = row.wa_media_id
        announced = row.mime_type
        # TX1 commits here - the claim becomes visible to every other attempt -
        # and the connection goes back to the pool for the whole of the fetch.
        async with released(self._session):
            fetched = await self._fetch(whatsapp, wa_media_id, announced=announced)

        row_or_none = await self._fenced(media.id)
        if row_or_none is None:
            return await self._superseded(media.id)
        row = row_or_none

        if fetched.refusal is not None:
            return await self._finish(row, fetched.refusal)
        refusal = await self._lifecycle_refusal(row)
        if refusal is not None:
            # Suspended, deleted or released while the file was on the wire.
            # The bytes are dropped here; no object is written for it.
            return await self._finish(row, refusal)
        downloaded = fetched.downloaded
        if downloaded is None:  # pragma: no cover - `_fetch` returns one or the other
            return await self._finish(row, MediaReason.DOWNLOAD_FAILED)
        row.downloaded_at = datetime.now(UTC)

        # The second trust boundary SEC-09 named. Everything before the fetch
        # decided what to do from Meta's descriptor, which is a claim about a
        # file, not the file. From here the bytes decide: a file whose contents
        # contradict what it was announced as, or that is not a supported
        # format at all, is skipped rather than stored and read as the thing it
        # claimed to be.
        try:
            detected = resolve_media_type(
                claimed=downloaded.mime_type or fetched.declared_type or announced,
                prefix=downloaded.content[:SNIFF_BYTES],
            )
        except MediaTypeError:
            logger.warning(
                "media.type_mismatch",
                extra={
                    "event": "media.type_mismatch",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(row.id),
                },
            )
            return await self._finish(row, MediaReason.TYPE_MISMATCH)

        # The intent. The object does not exist yet and the database already
        # knows its name, its size and the hash of what belongs in it - and,
        # since the capacity check runs under the same lock and commits with
        # it, the intent is also this upload's claim on the workspace's storage.
        try:
            key = await self.intend(row, mime_type=detected.mime_type, data=downloaded.content)
        except PlanLimitExceededError:
            # Skipped, not failed, and the message it arrived with is untouched.
            # A customer's WhatsApp message is never refused for the business's
            # billing (ADR-030); what is refused is writing another object into
            # a store the workspace has already filled.
            return await self._finish(row, MediaReason.CAPACITY)
        if key is None:
            return await self._finish(row, MediaReason.UPLOAD_CONFLICT)

        # No transaction, no pooled connection, no row lock across the write.
        # `released` commits what is staged, which is exactly the intent above
        # and is why it is safe here (ADR-080).
        async with released(self._session):
            written = await self._write(
                key=key, data=downloaded.content, mime_type=detected.mime_type
            )

        row_or_none = await self._fenced(media.id)
        if row_or_none is None:
            return await self._superseded(media.id)
        row = row_or_none
        if not written:
            # The intent stays. A row that names an object nobody managed to
            # write is the recoverable state, and reconciliation decides
            # afterwards whether it is there.
            return await self._finish(row, MediaReason.STORAGE_FAILED)

        return await self.finalize(row, key=key)

    async def _fetch(
        self, whatsapp: WhatsAppClient, wa_media_id: str, *, announced: str | None
    ) -> _Fetched:
        """The network half of a download: Meta's descriptor, then the body.

        Called with no transaction open. Returns bytes or a reason, never
        raises for anything a provider can do: the descriptor lookup is inside
        the same boundary as the body (MEDIA-04), and both run under one
        wall-clock deadline (MEDIA-10) - the client's own timeouts are per
        read, and a host dripping a byte inside each of them used to have no
        end at all.

        Every failure here is terminal for this attempt. The client has already
        spent its bounded retries on the transient ones (PD-MEDIA-08); a
        permanent answer - the file is gone, the credential is refused, the
        host is not Meta's - was never going to change.
        """
        cap = self._settings.media_max_bytes
        try:
            async with asyncio.timeout(self._settings.media_download_deadline_seconds):
                # Asked before fetching, not after. The alternative to asking is
                # paying to move a file in order to discover it was too big to
                # keep.
                descriptor = await whatsapp.probe_media(wa_media_id)
                declared_type = descriptor.mime_type or announced
                if descriptor.byte_size is not None and descriptor.byte_size > cap:
                    return _Fetched(refusal=MediaReason.OVERSIZE)
                if not self._is_readable(declared_type):
                    return _Fetched(refusal=MediaReason.UNSUPPORTED_TYPE)
                downloaded = await whatsapp.fetch_media(wa_media_id, max_bytes=cap)
        except TimeoutError:
            logger.warning(
                "media.download_timed_out",
                extra={
                    "event": "media.download_timed_out",
                    "tenant_id": str(self._tenant_id),
                    "deadline_seconds": self._settings.media_download_deadline_seconds,
                },
            )
            return _Fetched(refusal=MediaReason.TIMEOUT)
        except MediaTooLargeError:
            # Meta's declared size is a claim, and this is the branch where the
            # claim was wrong: the read was abandoned mid-body rather than
            # completed and then measured, so the worker never held more than
            # the cap.
            return _Fetched(refusal=MediaReason.OVERSIZE)
        except MediaUnavailableError:
            return _Fetched(refusal=MediaReason.UNAVAILABLE)
        except MediaCredentialRefusedError:
            return _Fetched(refusal=MediaReason.CREDENTIAL_REFUSED)
        except MediaHostRefusedError:
            return _Fetched(refusal=MediaReason.HOST_REFUSED)
        except MalformedMediaDescriptorError:
            return _Fetched(refusal=MediaReason.MALFORMED_DESCRIPTOR)
        except RateLimitedError:
            return _Fetched(refusal=MediaReason.RATE_LIMITED)
        except ExternalServiceError:
            return _Fetched(refusal=MediaReason.DOWNLOAD_FAILED)
        except Exception as error:
            # Anything a client raised that nobody listed. It costs this file,
            # not the conversation - the same boundary the reader has.
            logger.warning(
                "media.download_failed_unexpectedly",
                extra={
                    "event": "media.download_failed_unexpectedly",
                    "tenant_id": str(self._tenant_id),
                    "error_type": type(error).__name__,
                },
            )
            return _Fetched(refusal=MediaReason.DOWNLOAD_FAILED)
        return _Fetched(downloaded=downloaded, declared_type=declared_type)

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

        Raises `PlanLimitExceededError` when the workspace has no room left,
        which is a different answer from `None` and wants a different outcome
        on the row: `None` means "this file conflicts with what is recorded",
        and this means "there is nowhere to put it". Raised rather than
        returned as a second sentinel, because a caller that confused the two
        would write the wrong reason onto a customer's attachment - and
        because the authenticated upload path wants it to become a 402.

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

        # Asked here, and only for a row that is about to start occupying
        # space: the two branches above either reuse an intent that is already
        # counted or refuse outright, and charging capacity for those would
        # count the same bytes twice.
        #
        # `reserve` holds the workspace's advisory lock until this transaction
        # commits, which is a few statements away - the caller commits the
        # intent and does the object write outside any transaction. So two
        # uploads racing for the last megabyte serialise for the length of an
        # UPDATE rather than for the length of a network write.
        capacity = await self._entitlements.reserve(LimitKey.STORAGE_BYTES, additional=byte_size)
        if not capacity.allowed:
            raise PlanLimitExceededError(text_for(MediaReason.CAPACITY))

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
            if row.is_stored:
                # Reconciliation adopted the object while this attempt was
                # writing it. The storage state is right; the status still says
                # the download is in flight, and a row left like that is one
                # nothing would ever read.
                self._settle_stored(row)
                await self._session.flush()
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

    async def _write(self, *, key: str, data: bytes, mime_type: str) -> bool:
        """Put the bytes at the key the intent named. Never raises.

        A refusal is a `False` rather than an exception because the caller is
        inside `released`, where nothing may touch the session - and the row
        that has to record the failure is on the other side of that block.
        """
        try:
            await self._storage.put_at(key=key, data=data, mime_type=mime_type)
        except StorageError:
            return False
        return True

    async def understand(self, media: MessageMedia, *, reader: MediaReader) -> MediaOutcome:
        """Work out what a stored file says, and record it.

        - Read: `READY`, with the transcript.
        - Nothing to read - a silent recording, a scanned page, a PDF past the
          bounded parser's limits: `SKIPPED`. The file was opened and there was
          no text in it, and no retry changes that.
        - The attempt broke - the store, a provider after its own retries, the
          deadline, or a reader raising something unexpected: `FAILED`, and
          final (PD-MEDIA-08). The customer is answered either way.

        The store read and the reader both run with no transaction open, under
        one deadline, so neither a slow provider nor the PDF child's wait holds
        a pooled connection (MEDIA-11).
        """
        row = await self._media.lock_for_upload(media.id)
        if row.is_purged or row.status is MediaStatus.READY:
            # Read already - and, if purged, the file since removed. The
            # transcript on the row is the answer; paying a provider again, or
            # re-deriving it from bytes that are gone, would be waste.
            return self._as_it_stands(row)
        if row.status in TERMINAL_STATUSES:
            return self._as_it_stands(row)
        if not row.is_stored or row.storage_key is None:
            # A row mid-upload has a key and no proven object. Reading from it
            # would consume a write that has not been finalised (ADR-087).
            return await self._finish(row, MediaReason.NOTHING_STORED)
        refusal = await self._lifecycle_refusal(row)
        if refusal is not None:
            # No vision, transcription or PDF parse for a workspace that is no
            # longer served. The object already stored stays, under the same
            # retention and purge as every other stored file.
            return await self._finish(row, refusal)

        if self._claim_id is None or row.claim_id != self._claim_id:
            claimed = await self._claim(row, status=MediaStatus.STORED)
            if claimed is not None:
                return claimed

        key = row.storage_key
        mime_type = row.mime_type
        async with released(self._session):
            result, refusal, decision = await self._read_stored(
                key=key, mime_type=mime_type, reader=reader
            )

        if result is not None:
            # One file read, whatever it took to read it - metered even if this
            # attempt has since lost its claim, because the provider was paid
            # either way. Transcription is metered separately because it is a
            # second provider and priced as one - but as a count of recordings,
            # not their length: the configured models report no duration, and
            # inferring seconds from a compressed byte count would put a
            # fabricated number in a bill.
            self._usage.record(
                UsageEventType.MEDIA_PROCESSING,
                meta={"media_id": str(media.id), "method": result.method},
            )
            if result.method == TRANSCRIPTION_METHOD:
                self._usage.record(
                    UsageEventType.VOICE_TRANSCRIPTION,
                    meta={"media_id": str(media.id), "byte_size": row.byte_size},
                )

        row_or_none = await self._fenced(media.id)
        if row_or_none is None:
            return await self._superseded(media.id)
        row = row_or_none

        if refusal is not None:
            return await self._finish(row, refusal, text=decision)
        if result is None:  # pragma: no cover - `_read_stored` returns one or the other
            return await self._finish(row, MediaReason.READER_FAILED)

        logger.info(
            "media.read",
            extra={
                "tenant_id": str(self._tenant_id),
                "media_id": str(row.id),
                "method": result.method,
            },
        )
        return await self.mark_ready(row, transcript=result.transcript)

    async def _read_stored(
        self, *, key: str, mime_type: str | None, reader: MediaReader
    ) -> tuple[ReadResult | None, MediaReason | None, str | None]:
        """Read a stored object back and understand it, with no transaction open.

        Returns the result, or the reason and - for a reader's own decision -
        its fixed sentence. Never raises for anything the store or a reader can
        do; `Exception` rather than `BaseException`, so cancellation and
        interpreter exit still leave.
        """
        try:
            async with asyncio.timeout(self._settings.media_understanding_deadline_seconds):
                content = await self._storage.get(key)
                return await reader.read(content=content, mime_type=mime_type), None, None
        except TimeoutError:
            return None, MediaReason.TIMEOUT, None
        except StorageError:
            return None, MediaReason.STORAGE_FAILED, None
        except _READER_DECISIONS as decision:
            return None, MediaReason.UNREADABLE, decision.message
        except (ExternalServiceError, RateLimitedError):
            return None, MediaReason.PROVIDER_FAILED, None
        except Exception as error:
            # The containment boundary (MEDIA-03). A reader raising something
            # nobody anticipated - a parser, a provider client, a bug - costs
            # this file's reading and nothing else: not the conversation, whose
            # reply is gated on this row resolving, and not the queue. The
            # error's type is logged and its text is not: a message raised
            # while reading a customer's file can quote the file.
            logger.warning(
                "media.reader_failed",
                extra={
                    "event": "media.reader_failed",
                    "tenant_id": str(self._tenant_id),
                    "error_type": type(error).__name__,
                },
            )
            return None, MediaReason.READER_FAILED, None

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
        media.claim_id = None
        await self._session.flush()
        return MediaOutcome(media_id=media.id, status=MediaStatus.READY)

    # ------------------------------------------------------ giving a file up

    async def abandon(self, media_id: uuid.UUID, *, claim_id: uuid.UUID | None) -> MediaOutcome:
        """Make an unresolved file terminal because nothing will finish it (MEDIA-03).

        Called when a media job is dead-lettered. Retry-safe by construction: a
        row already resolved is returned as it stands, so terminalising twice
        writes once.

        A row somebody else holds a *live* claim on is left alone - another
        attempt is working on it, and the job that died was a duplicate. The
        claim `claim_id` names is the dead job's own, if it got that far, and is
        treated as no claim at all: nobody is going to honour it.
        """
        row = await self._media.lock_for_upload(media_id)
        if row.status in TERMINAL_STATUSES:
            return self._as_it_stands(row)
        if row.claim_id is not None and row.claim_id != claim_id and self._claim_is_live(row):
            return MediaOutcome(media_id=row.id, status=row.status, deferred=True)
        return await self._finish(row, MediaReason.ABANDONED)

    # ------------------------------------------------------------ lifecycle

    async def _lifecycle_refusal(self, row: MessageMedia) -> MediaReason | None:
        """Why this file may not be processed now, or None if it may (MEDIA-08).

        Asked fresh before each thing that costs something - Meta, the object
        store, a paid read - because the answer can change between them. A
        closed conversation or a disabled agent is deliberately *not* a
        refusal: the file is still the customer's message and is kept, and
        whether anybody answers it is the agent worker's decision (AI-06).
        """
        serving = await self._media.serving(row.conversation_id)
        if serving is None:
            return MediaReason.CHANNEL_UNAVAILABLE
        if serving.workspace_deleted:
            return MediaReason.WORKSPACE_DELETED
        if not serving.workspace_active:
            return MediaReason.WORKSPACE_SUSPENDED
        if not serving.channel_available:
            return MediaReason.CHANNEL_UNAVAILABLE
        return None

    async def _client_for(self, row: MessageMedia) -> WhatsAppClient | None:
        """A client carrying the credential of the number this file arrived on.

        The same authority model outbound sends use (ADR-034): a number the
        workspace connected with its own token is fetched with that token, and
        one without is fetched with the platform's. A workspace token this
        process cannot decrypt is **not** downgraded to the platform's -
        fetching as somebody else is a different act - and ends the file
        without a fetch. The token lives only in the client built here; it is
        never written to the row, a log line or anything an agent reads.
        """
        if self._whatsapp_for is None:
            return None
        account = await self._media.account_for(row.conversation_id)
        if account is None:
            return None
        try:
            resolved = self._credentials.resolve(account)
        except CredentialDecryptionError:
            logger.error(
                "media.credential_unreadable",
                extra={
                    "event": "media.credential_unreadable",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(row.id),
                },
            )
            return None
        if not resolved.token:
            logger.warning(
                "media.credential_missing",
                extra={
                    "event": "media.credential_missing",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(row.id),
                },
            )
            return None
        logger.info(
            "media.credential_resolved",
            extra={
                "event": "media.credential_resolved",
                "tenant_id": str(self._tenant_id),
                "media_id": str(row.id),
                "workspace_credential": resolved.is_own,
            },
        )
        return self._whatsapp_for(resolved.token)

    # --------------------------------------------------------------- claims

    async def _claim(self, row: MessageMedia, *, status: MediaStatus) -> MediaOutcome | None:
        """Take this file for this attempt, or say why not. Staged; the caller commits.

        Returns None when the claim is taken. Otherwise returns what the
        attempt should report instead:

        - somebody else's claim is live, so this attempt stands aside and
          writes nothing - a duplicate job costs no second download or read;
        - the file has already been taken up `MAX_ATTEMPTS` times without
          finishing, so it is given up on rather than tried again - a file
          that kills its worker every time cannot loop for ever.
        """
        if row.claim_id is not None and self._claim_is_live(row):
            logger.info(
                "media.claim_held_elsewhere",
                extra={
                    "event": "media.claim_held_elsewhere",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(row.id),
                },
            )
            return MediaOutcome(media_id=row.id, status=row.status, deferred=True)
        if row.is_exhausted:
            return await self._finish(row, MediaReason.ABANDONED)

        self._claim_id = uuid.uuid4()
        row.claim_id = self._claim_id
        row.claimed_at = datetime.now(UTC)
        row.attempts += 1
        row.status = status
        await self._session.flush()
        return None

    async def _fenced(self, media_id: uuid.UUID) -> MessageMedia | None:
        """The row, re-read under its lock, if this attempt still holds it.

        None means the claim is gone - the recovery sweep decided this attempt
        was dead, or another attempt took over - and this one must write
        nothing further. `populate_existing` in the repository is what makes
        the comparison against the database rather than against this
        session's memory of the row.
        """
        row = await self._media.lock_for_upload(media_id)
        if self._claim_id is None or row.claim_id != self._claim_id:
            logger.warning(
                "media.claim_lost",
                extra={
                    "event": "media.claim_lost",
                    "tenant_id": str(self._tenant_id),
                    "media_id": str(media_id),
                },
            )
            return None
        return row

    async def _superseded(self, media_id: uuid.UUID) -> MediaOutcome:
        row = await self._media.lock_for_upload(media_id)
        return MediaOutcome(media_id=row.id, status=row.status, deferred=True)

    def _claim_is_live(self, row: MessageMedia) -> bool:
        if row.claimed_at is None:
            return False
        return datetime.now(UTC) - row.claimed_at < claim_lease(self._settings)

    # ------------------------------------------------------ recording outcomes

    async def _finish(
        self, row: MessageMedia, reason: MediaReason, *, text: str | None = None
    ) -> MediaOutcome:
        """Put a row in its terminal state for `reason`, with a Wasla sentence.

        `text` is only ever a reader decision's own fixed message; everything
        else takes the vocabulary's sentence. Bounded either way, far inside the
        column, so no sentence can be the thing that fails the write.
        """
        status = status_for(reason)
        sentence = (text or text_for(reason))[:MAX_REASON_LENGTH]
        row.status = status
        row.last_error = sentence
        row.processed_at = datetime.now(UTC)
        row.claim_id = None
        await self._session.flush()

        log = logger.info if status is MediaStatus.SKIPPED else logger.warning
        log(
            "media.skipped" if status is MediaStatus.SKIPPED else "media.failed",
            extra={
                "event": "media.resolved",
                "tenant_id": str(self._tenant_id),
                "media_id": str(row.id),
                "reason": str(reason),
                "attempts": row.attempts,
            },
        )
        return MediaOutcome(media_id=row.id, status=status, detail=sentence, reason=reason)

    @staticmethod
    def _settle_stored(row: MessageMedia) -> None:
        if row.status in (MediaStatus.PENDING, MediaStatus.DOWNLOADING):
            row.status = MediaStatus.STORED

    @staticmethod
    def _as_it_stands(row: MessageMedia) -> MediaOutcome:
        return MediaOutcome(media_id=row.id, status=row.status, detail=row.last_error)

    def _is_readable(self, mime_type: str | None) -> bool:
        return mime_type is not None and mime_type.lower() in READABLE_TYPES
