"""Removing stored files once they are older than a deployment says they may be.

Nothing ever deleted a stored file. `MediaStorage.delete` existed and no caller
invoked it, so the media volume grew monotonically - which on a single host is a
disk-full incident with no warning that takes the API and the worker down
together, because they share the volume (BUG-006).

## What this deletes, and what it does not

The **file**, never the row. `message_media` carries the transcript - what Wasla
concluded a photograph or a voice note said - and that is conversation history:
it is what the agent was shown and what a colleague reading the thread sees.
Deleting it would silently rewrite the record of a conversation. What retention
removes is the copy of the original bytes, and the row afterwards says plainly
that there was a file and that it was removed.

## Why the deletion is two writes and not one

Removing an object and clearing the column that points at it are writes to two
systems, and no transaction spans both. Both orders can fail, and they fail
differently:

    delete object, then commit     ->  commit fails, and the row now points
                                       confidently at a file that is gone.
                                       Indistinguishable from a broken store.

    commit, then delete object     ->  the delete fails, and nothing anywhere
                                       remembers the object exists. A permanent
                                       orphan that no query can find.

So the claim is committed *first*, and it is what makes the middle recoverable:
`purge_started_at` set with `storage_key` still present means "this file is
being removed, and may or may not still be there". A pass that dies anywhere
leaves that state, and the next pass deletes again - which is a no-op on an
object already gone, in both backends - and finishes the job.

The cost is a window in which a row says a file is going and the file is still
readable. That is the right way round: a colleague briefly seeing an attachment
that was due for deletion is a smaller failure than a colleague being told a
file is there when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.storage import MediaStorage, StorageError
from app.db.models.media import MessageMedia
from app.repositories.media_repository import PlatformMediaRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What one sweep did.

    `claimed` and `purged` are separate numbers because they diverge exactly
    when something is wrong: a pass that claims fifty files and purges forty
    means ten object deletions failed, and the difference is the signal.
    `already_claimed` counts rows a previous pass left mid-flight, which is how
    an operator sees a store that has been refusing deletions for a while.
    """

    claimed: int = 0
    purged: int = 0
    already_claimed: int = 0
    failed: int = 0


class MediaRetentionService:
    """Deletes stored files whose retention period has passed.

    Not tenant-scoped, and deliberately: this is a platform sweep over every
    workspace, in the same shape as the billing worker's. Every method takes the
    rows it was given rather than a tenant id, so there is no scoped repository
    here for a caller to mistake for an authorization boundary - the
    authorization question does not arise, because no request reaches this.
    """

    def __init__(self, *, session: AsyncSession, storage: MediaStorage) -> None:
        self._session = session
        self._storage = storage
        self._media = PlatformMediaRepository(session)

    async def claim(
        self,
        *,
        now: datetime | None = None,
        retention_days: int,
        limit: int,
    ) -> list[MessageMedia]:
        """Mark files past their retention as being removed, and commit that.

        Returns the rows to purge, which includes anything an earlier pass had
        already claimed and not finished. Committing here rather than at the end
        of the whole sweep is the point of the two phases: the claim has to
        survive this process dying in the middle of the deletions below.

        A retention of zero disables the sweep entirely and returns nothing. It
        is the default, because how long a business keeps what its customers
        sent it is not a decision this code can make - any number here would be
        invented, and an invented number that deletes customer data is worse
        than no sweep at all.
        """
        if retention_days <= 0:
            return []

        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=retention_days)

        rows = await self._media.due_for_purge(cutoff=cutoff, limit=limit)
        for media in rows:
            if media.purge_started_at is None:
                media.purge_started_at = moment
        await self._session.commit()
        return rows

    async def purge(self, media: MessageMedia) -> bool:
        """Remove one claimed file's object and finish its row.

        Returns whether the object is now gone. Idempotent in both directions:
        deleting an object that was already deleted is a no-op in both backends,
        and a row whose key is already cleared is skipped without touching the
        store.

        A failure leaves the row exactly as it was - claimed, key intact - so
        the next pass tries again. Nothing about a failed deletion is written
        to the row, because a `last_error` here would be describing the store's
        health on a row that describes a customer's file.
        """
        if media.storage_key is None:
            return True

        key = media.storage_key
        try:
            await self._storage.delete(key)
        except StorageError:
            logger.warning(
                "media.purge_failed",
                extra={
                    "event": "media.purge_failed",
                    "tenant_id": str(media.tenant_id),
                    "media_id": str(media.id),
                },
            )
            return False

        # Cleared only after the object is definitely gone. This is the write
        # that makes the row terminal, and doing it first would be the second
        # of the two failure orders in this module's docstring.
        media.storage_key = None
        logger.info(
            "media.purged",
            extra={
                "event": "media.purged",
                "tenant_id": str(media.tenant_id),
                "media_id": str(media.id),
            },
        )
        return True

    async def sweep(
        self,
        *,
        now: datetime | None = None,
        retention_days: int,
        limit: int,
    ) -> RetentionOutcome:
        """One pass: claim what is due, then remove each object.

        The commit at the end covers the cleared keys. A failure part-way
        through leaves the rows it did not reach claimed but intact, which is
        the state the next pass is built to continue from.
        """
        rows = await self.claim(now=now, retention_days=retention_days, limit=limit)
        if not rows:
            return RetentionOutcome()

        moment = now or datetime.now(UTC)
        resumed = sum(1 for media in rows if media.purge_started_at != moment)

        purged = 0
        failed = 0
        for media in rows:
            if await self.purge(media):
                purged += 1
            else:
                failed += 1

        await self._session.commit()
        return RetentionOutcome(
            claimed=len(rows),
            purged=purged,
            already_claimed=resumed,
            failed=failed,
        )

    async def pending_count(self) -> int:
        """How many claimed files still have an object.

        Published as a metric rather than merely logged. A number that stays
        above zero across sweeps is a store refusing deletions, which is
        otherwise invisible: the rows are claimed, the sweep reports itself as
        having run, and the volume does not shrink.
        """
        return await self._media.pending_purge_count()

    async def reconcile(self, *, limit: int) -> int:
        """Finish rows a previous pass claimed and never completed.

        Distinct from the age query, because a claimed row can outlive its own
        eligibility window: raise `MEDIA_RETENTION_DAYS` after a failed pass and
        the ordinary sweep stops selecting the rows it left half-done, which
        would strand them - claimed for ever, object never removed, and no query
        looking for them.

        This is the only orphan handling that is honest here. The reverse
        direction - an object in the bucket that no row references - would mean
        listing the store and deciding from age, and a sweep that deleted by
        that rule would eventually delete a live file whose row it had failed to
        read. See ADR-078 for what is left open and why.
        """
        rows = await self._media.claimed_but_unfinished(limit=limit)
        finished = 0
        for media in rows:
            if await self.purge(media):
                finished += 1
        await self._session.commit()
        return finished


def purge_reason(media: MessageMedia) -> str | None:
    """What to tell somebody whose attachment is no longer there.

    Returns None for a file that is present, so a caller can use this to decide
    *whether* to explain as well as what to say.
    """
    if not media.is_purged:
        return None
    return "This file was removed under this workspace's media retention policy."


__all__ = ["MediaRetentionService", "RetentionOutcome", "purge_reason"]
