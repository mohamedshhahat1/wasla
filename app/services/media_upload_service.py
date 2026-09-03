"""Finishing object writes that were interrupted, without looking in the bucket.

An object and the row that owns it live in two systems, and no transaction
spans them. Retention already deals with one direction - remove the object,
then clear the reference - and this is the other: **write the reference, then
create the object**. Both orders of the pair fail, and the failure this closes
was stated and left open in ADR-078:

    put the object, then commit    ->  the commit fails and nothing anywhere
                                       remembers the object exists. A permanent
                                       orphan no query can find.

    commit, then put the object    ->  the write fails and a row names an object
                                       that is not there. Recoverable, because
                                       the row can be asked about.

So the intent is committed *first* (`MediaService.intend`), and this module is
what makes the middle recoverable. A row in `PENDING` says: Wasla decided to
write exactly this object, of exactly this size, whose contents hash to exactly
this - and does not yet know whether the write landed. Every recovery starts
from that sentence.

## What this deliberately is not

**It never lists the bucket.** The tempting design is to enumerate objects,
subtract the keys the database knows about, and delete the remainder. That
sweep is unsafe in a way that does not show up until it has already destroyed
something: a PostgreSQL failure, a replica lagging, a query timing out, a row
this process could not read for any reason at all - each makes a live
attachment look like an orphan, and the rule says delete it. Deletion by
*absence of evidence* cannot be made safe, so the only keys this module knows
are the ones a committed row names, and the only objects it can act on are
those.

**It does not decide whether a file is readable.** `MediaStatus` is the media
queue's column, with its own bounded retries and its own dead-letter list. This
owns `storage_state` and nothing else. Two states, two owners; a reconciler
that also moved `MediaStatus` would be racing the worker for it.

## The three answers, and why the third is not the second

Asking the store where an object is has three outcomes, not two:

    present  + contents match   ->  finalise. The write did land.
    present  + contents differ  ->  quarantine. Do not serve it, do not delete
                                    it: deleting destroys the only evidence of
                                    how a foreign object reached our key.
    absent                      ->  abandon. There is nothing there, and the
                                    bytes to try again with are gone.
    unreachable                 ->  nothing. Not "absent". A store that is down
                                    read as "the object is gone" would abandon
                                    every upload in flight during the outage.

The fourth line is why `MediaStorage.exists` raises rather than returning
False, and why 403 is not treated as a missing object (`object_store.py`).

## The trust model for "contents match"

Recomputed SHA-256 over the bytes read back, compared with the hash the intent
committed before the write. Not the ETag: S3 defines it as an opaque validator,
it is the MD5 of the body only for a single-part unencrypted upload, and a
store that computes it differently - or a bucket with SSE-KMS - would make
every object look wrong. Not a header either: `x-amz-meta-sha256` is a claim
travelling with the object rather than a fact about it.

Reading the object back costs a GET of at most `MEDIA_MAX_BYTES`, and this runs
only for writes that were interrupted - which on a healthy deployment is never.
Paying a full read on the rare path to avoid trusting a validator on every one
is the right way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.storage import MediaStorage, StorageError
from app.db.models.media import MediaStorageState, MessageMedia
from app.db.models.usage import UsageEventType
from app.repositories.media_repository import PlatformMediaRepository
from app.services.media_service import content_hash
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)


class Verdict(StrEnum):
    """What one stale intent turned out to be.

    A bounded set, because it is a metric label. Five values, for ever.
    """

    FINALIZED = "finalized"
    MISSING = "missing"
    MISMATCHED = "mismatched"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What one pass did.

    Every number here is a count of *logical* outcomes rather than of attempts,
    so two workers running at once produce one increment between them for a row
    only one of them claimed.
    """

    finalized: int = 0
    missing: int = 0
    mismatched: int = 0
    unreachable: int = 0

    @property
    def examined(self) -> int:
        return self.finalized + self.missing + self.mismatched + self.unreachable


class MediaUploadReconciler:
    """Settles upload intents whose object write never reported back.

    Not tenant-scoped, in the same shape as `MediaRetentionService` and for the
    same reason: this is a platform sweep over every workspace, and nothing
    reachable from a request constructs it. The authorization question does not
    arise - but the *ownership* question still does, and it is answered by the
    row rather than by the key. A workspace's media is reconciled because a row
    carrying its `tenant_id` says an object was intended, never because a key
    happens to start with a tenant prefix. Key layout is not an authorization
    boundary and is not read as one here.
    """

    def __init__(self, *, session: AsyncSession, storage: MediaStorage) -> None:
        self._session = session
        self._storage = storage
        self._media = PlatformMediaRepository(session)

    async def run(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float,
        limit: int,
    ) -> ReconciliationOutcome:
        """One pass over intents old enough to be nobody's live work.

        The grace period is the whole of the concurrency story with the
        original writer. An intent committed a second ago belongs to a request
        or job that is very probably between its PUT and its finalisation, and
        verifying it there would race a write that is about to happen anyway -
        finding the object absent, abandoning the row, and then having the
        finalisation land on a row this pass has just changed. Waiting is
        cheaper and more obviously correct than coordinating.

        Each row is claimed with `FOR UPDATE SKIP LOCKED` and settled in its
        own transaction, so a second worker skips what this one holds and a
        store that hangs on one object does not hold a lock on the others.
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=grace_seconds)

        finalized = missing = mismatched = unreachable = 0
        for _ in range(limit):
            verdict = await self._settle_one(cutoff=cutoff)
            if verdict is None:
                break
            if verdict is Verdict.FINALIZED:
                finalized += 1
            elif verdict is Verdict.MISSING:
                missing += 1
            elif verdict is Verdict.MISMATCHED:
                mismatched += 1
            else:
                # The store is not answering. Asking it about the next
                # ninety-nine objects will produce the same answer more slowly,
                # and the rows are all still here next pass.
                unreachable += 1
                break

        return ReconciliationOutcome(
            finalized=finalized,
            missing=missing,
            mismatched=mismatched,
            unreachable=unreachable,
        )

    async def _settle_one(self, *, cutoff: datetime) -> Verdict | None:
        """Claim the oldest unclaimed stale intent and settle it. None if there is none.

        One row per transaction rather than a batch, and the reason is the
        network call in the middle. A batch would hold every claimed row's lock
        for the length of every verification in it, so one slow object would
        block a sibling worker from the rest - and a batch that failed halfway
        would roll back the verdicts it had already reached.

        The lock is released by the commit at the end of this method, which is
        also what publishes the verdict. Nothing here holds a lock across the
        store call *and* a subsequent decision: the read happens while the row
        is locked, which is deliberate - it is what stops a second reconciler
        finalising the same intent - and it is one bounded GET against
        infrastructure on the deployment network, not a provider inference.
        """
        rows = await self._media.claim_stale_uploads(cutoff=cutoff, limit=1)
        if not rows:
            await self._session.rollback()
            return None

        media = rows[0]
        key = media.storage_key
        if key is None:
            # Unreachable: the check constraint refuses PENDING without a key.
            # Handled rather than asserted, because a row is input to whatever
            # reads it whatever wrote it.
            media.storage_state = MediaStorageState.ABSENT
            await self._session.commit()
            return Verdict.MISSING

        verdict = await self._verify(media, key=key)
        if verdict is Verdict.UNREACHABLE:
            # Nothing decided, nothing written. The row keeps its intent and
            # the next pass asks again.
            await self._session.rollback()
            return verdict

        self._apply(media, verdict)
        await self._session.commit()
        return verdict

    async def _verify(self, media: MessageMedia, *, key: str) -> Verdict:
        """Ask the store what is at this exact key, and whether it is ours.

        HEAD first, because it is the only call that distinguishes the three
        answers cheaply: a store that is down raises here rather than being
        mistaken for one that has nothing. Then the bytes, because existence is
        not identity - an object of the right size at the right key can still
        be the wrong file.
        """
        try:
            present = await self._storage.exists(key)
        except StorageError:
            logger.warning(
                "media.upload_unverifiable",
                extra={
                    "event": "media.upload_unverifiable",
                    "tenant_id": str(media.tenant_id),
                    "media_id": str(media.id),
                },
            )
            return Verdict.UNREACHABLE

        if not present:
            return Verdict.MISSING

        try:
            content = await self._storage.get(key)
        except StorageError:
            # It was there a moment ago. Whatever this is, it is not evidence
            # that the object is gone.
            return Verdict.UNREACHABLE

        if len(content) != media.byte_size or content_hash(content) != media.content_hash:
            return Verdict.MISMATCHED
        return Verdict.FINALIZED

    def _apply(self, media: MessageMedia, verdict: Verdict) -> None:
        """Write the verdict onto the row. Never `UNREACHABLE`, which decides nothing."""
        if verdict is Verdict.FINALIZED:
            media.storage_state = MediaStorageState.STORED
            # Metered by whoever finalises, exactly once: the transition out of
            # PENDING happens under the row lock this holds, so the original
            # writer and this pass cannot both record it.
            UsageRecorder(self._session, tenant_id=media.tenant_id).record(
                UsageEventType.STORAGE_USED,
                quantity=media.byte_size,
                meta={"media_id": str(media.id), "recovered": "true"},
            )
            logger.info(
                "media.upload_recovered",
                extra={
                    "event": "media.upload_recovered",
                    "tenant_id": str(media.tenant_id),
                    "media_id": str(media.id),
                    "byte_size": media.byte_size,
                },
            )
            return

        if verdict is Verdict.MISMATCHED:
            # Left where it is, and taken out of everybody's way. Serving it
            # would hand a colleague a file the row does not describe; deleting
            # it would destroy the only copy of how it got there.
            media.storage_state = MediaStorageState.MISMATCHED
            logger.error(
                "media.upload_mismatch",
                extra={
                    "event": "media.upload_mismatch",
                    "tenant_id": str(media.tenant_id),
                    "media_id": str(media.id),
                },
            )
            return

        # MISSING. The write never landed and the bytes are gone - an inbound
        # file can be fetched from Meta again by its own job, and an outbound
        # one arrived in a request body that no longer exists. Either way this
        # row owns no object, and saying so is what lets a later attempt
        # allocate a fresh key without leaking the one it did not use.
        media.storage_key = None
        media.storage_state = MediaStorageState.ABSENT
        media.upload_started_at = None
        logger.warning(
            "media.upload_missing",
            extra={
                "event": "media.upload_missing",
                "tenant_id": str(media.tenant_id),
                "media_id": str(media.id),
            },
        )

    async def pending_count(self) -> int:
        """How many upload intents are outstanding, across every workspace."""
        return await self._media.pending_upload_count()

    async def mismatched_count(self) -> int:
        """How many objects were found to disagree with the row that owns them."""
        return await self._media.mismatched_count()


__all__ = [
    "MediaUploadReconciler",
    "ReconciliationOutcome",
    "Verdict",
]
