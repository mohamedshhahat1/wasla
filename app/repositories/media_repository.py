"""Data access for attached files.

`MediaRepository` is tenant-scoped like everything a request touches.
`ConversationMediaGate` is the one thing here that takes a lock, and it is a
separate class for the same reason `DueFollowUpClaim` is: locking a conversation
row is not an ordinary read, and a method that quietly did it from the middle of
a repository would be found by accident rather than on purpose.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation
from app.db.models.media import (
    UNRESOLVED_MEDIA_STATUSES,
    MediaStatus,
    MediaStorageState,
    MessageMedia,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


class MediaRepository(TenantScopedRepository[MessageMedia]):
    """Attached files of one workspace."""

    model = MessageMedia

    def _tenant_filter(self) -> ColumnElement[bool]:
        return MessageMedia.tenant_id == self.tenant_id

    async def get_by_id(self, media_id: uuid.UUID) -> MessageMedia | None:
        return await self._first(self._select().where(MessageMedia.id == media_id))

    async def require_by_id(self, media_id: uuid.UUID) -> MessageMedia:
        return await self._require(self._select().where(MessageMedia.id == media_id))

    async def get_for_message(self, message_id: uuid.UUID) -> MessageMedia | None:
        return await self._first(self._select().where(MessageMedia.message_id == message_id))

    async def lock_for_upload(self, media_id: uuid.UUID) -> MessageMedia:
        """Re-read this row under a row lock, so one object key is allocated once.

        The queue can deliver the same media job twice - a lease that expired
        while the worker was still holding it, a replay after a crash - and two
        attempts each allocating a key would each write an object, of which
        only one could end up on the row. The other would be an object with no
        owner, which is the exact failure ADR-087 exists to make impossible.

        Serialising the allocation is enough to prevent it: the second attempt
        waits, re-reads, finds the key the first one committed, and writes its
        bytes to that same key rather than to a new one.

        Tenant-scoped like every other read here, so the lock cannot be taken
        on a row belonging to somebody else. Held for the length of the intent
        transaction only, which makes no network call.

        `populate_existing` is not optional. Without it SQLAlchemy takes the
        lock, returns the instance already in the identity map, and leaves its
        attributes exactly as this session last saw them - so the caller would
        hold the lock and still be reading the values it was trying to
        re-check, which is the whole point of taking it.
        """
        return await self._require(
            self._select()
            .where(MessageMedia.id == media_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def list_for_conversation(self, conversation_id: uuid.UUID) -> list[MessageMedia]:
        return await self._all(
            self._select()
            .where(MessageMedia.conversation_id == conversation_id)
            .order_by(MessageMedia.created_at)
        )

    async def record(
        self,
        *,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        wa_media_id: str | None,
        mime_type: str | None,
        filename: str | None,
        is_voice: bool,
    ) -> tuple[MessageMedia, bool]:
        """Note that a message carries a file. Returns the row and whether it is new.

        A message already carrying one is returned as it stands. That is a
        webhook replay, and the unique constraint would refuse a second row
        anyway - checking first turns a crash into a no-op.
        """
        existing = await self.get_for_message(message_id)
        if existing is not None:
            return existing, False

        media = MessageMedia(
            tenant_id=self.tenant_id,
            message_id=message_id,
            conversation_id=conversation_id,
            wa_media_id=wa_media_id,
            status=MediaStatus.PENDING,
            mime_type=mime_type,
            filename=filename,
            is_voice=is_voice,
        )
        return self.add(media), True

    async def map_for_messages(
        self,
        message_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, MessageMedia]:
        """The files attached to these messages, keyed by message id.

        One query for a whole conversation window. The alternative - reaching
        through a relationship while rendering each message - is a query per
        message, and inside an async session it does not merely cost more, it
        raises.
        """
        if not message_ids:
            return {}

        rows = await self._all(self._select().where(MessageMedia.message_id.in_(message_ids)))
        return {row.message_id: row for row in rows}

    async def count_unresolved(self, conversation_id: uuid.UUID) -> int:
        """How many files on this conversation still owe it an answer."""
        statement = (
            select(func.count())
            .select_from(MessageMedia)
            .where(
                self._tenant_filter(),
                MessageMedia.conversation_id == conversation_id,
                MessageMedia.status.in_(UNRESOLVED_MEDIA_STATUSES),
            )
        )
        return int((await self._session.execute(statement)).scalar_one())


class ConversationMediaGate(BaseRepository[Conversation]):
    """Serialises the decision to let an agent answer a conversation.

    The problem this exists for: one webhook delivery can carry two photographs,
    which become two media jobs, possibly on two workers. Each finishes, each
    asks "is anything still unread here?", and if both ask at the same moment
    both see nothing and both ask an agent to reply. The customer gets two
    answers to one question, because an agent turn - unlike ingestion - is not
    idempotent.

    Taking a row lock on the conversation turns that race into a queue. The
    second worker waits for the first to commit, then counts, then sees the
    truth. It is the cheapest correct answer available: no new table, no Redis
    key, and the lock is held for a single count.
    """

    model = Conversation

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def lock(self, conversation_id: uuid.UUID) -> None:
        """Hold the conversation row until this transaction ends.

        Deliberately returns nothing. The row is not the point - the ordering
        is - and returning it would invite a caller to read it and believe the
        lock was about its contents.
        """
        await self._session.execute(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
        )


class PlatformMediaRepository(BaseRepository[MessageMedia]):
    """Attached files across every workspace, for the retention sweep.

    Unscoped, like `PlatformSubscriptionRepository` and for the same reason: a
    sweep that had to be told which workspace to look at would need a list of
    workspaces to iterate, which is a query per tenant to do the work of one.

    Kept as a separate class rather than a flag on `MediaRepository`, so that
    "this repository is not tenant-scoped" is a fact about the type and visible
    at every call site, instead of an argument somebody can pass by accident.
    Nothing reachable from a request constructs it.
    """

    model = MessageMedia

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def due_for_purge(self, *, cutoff: datetime, limit: int) -> list[MessageMedia]:
        """Fully stored files older than `cutoff`, plus anything mid-purge, oldest first.

        `STORED` and nothing looser. It used to be `storage_key IS NOT NULL`,
        which was the same set while a key could only exist once its object
        did; it is not the same set now that a key is committed before the
        write. An upload still in flight carries a key, is old enough the
        moment its message is, and deleting its object would be retention
        destroying a file nobody has finished writing - so `PENDING` is not
        here, and reconciliation owns those instead (ADR-087).

        `PURGING` is included for the same reason it always was: a claim that
        was not finished is exactly what the next pass has to pick up.

        Ordered oldest first so a backlog drains in the order it accumulated,
        and bounded so a deployment with a large one takes several passes
        rather than one enormous transaction.
        """
        statement = (
            select(MessageMedia)
            .where(
                MessageMedia.storage_state.in_(
                    (MediaStorageState.STORED, MediaStorageState.PURGING)
                ),
                MessageMedia.created_at < cutoff,
            )
            .order_by(MessageMedia.created_at)
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def claimed_but_unfinished(self, *, limit: int) -> list[MessageMedia]:
        """Rows a sweep claimed and never completed, whatever their age now.

        Separate from `due_for_purge` because a claimed row can outlive its own
        eligibility: raise the retention period after a failed pass and the age
        query stops selecting the rows it left half-done. They would then stay
        claimed for ever with their objects still in the store, and no query
        anywhere would be looking for them.
        """
        statement = (
            select(MessageMedia)
            .where(MessageMedia.storage_state == MediaStorageState.PURGING)
            .order_by(MessageMedia.purge_started_at)
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def pending_purge_count(self) -> int:
        """How many claimed files still have an object.

        Published as a metric. A number that stays above zero across sweeps is a
        store refusing deletions, which is otherwise invisible: the rows are
        claimed, the sweep reports itself as having run, and the volume does not
        shrink.
        """
        statement = (
            select(func.count())
            .select_from(MessageMedia)
            .where(MessageMedia.storage_state == MediaStorageState.PURGING)
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def claim_stale_uploads(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[MessageMedia]:
        """Take ownership of upload intents whose write never finished.

        `FOR UPDATE SKIP LOCKED`, so two reconcilers divide the work instead of
        both verifying and both finalising the same object. The lock is what
        makes the claim; there is no claim *column* here, unlike retention,
        because unlike a purge this decision is cheap to repeat and the rows
        are few - a healthy deployment has none. Adding a column would mean a
        write before the work and a second write to release it, on a query that
        normally returns nothing.

        `cutoff` is the grace period: an intent younger than it belongs to a
        request or job that is very probably still running, and reconciling
        underneath it would race the finalisation it is about to do.

        The lock is held only for the length of the claiming transaction, which
        contains no network call. Verification happens afterwards, against a
        connection this is not holding (ADR-080).
        """
        statement = (
            select(MessageMedia)
            .where(
                MessageMedia.storage_state == MediaStorageState.PENDING,
                MessageMedia.upload_started_at < cutoff,
            )
            .order_by(MessageMedia.upload_started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def pending_upload_count(self) -> int:
        """How many uploads are in flight or stuck, across every workspace.

        A level, published like `pending_purge_count` is. Zero is the healthy
        reading; a number that stays above it across passes is a store that is
        accepting neither writes nor questions about them.
        """
        statement = (
            select(func.count())
            .select_from(MessageMedia)
            .where(MessageMedia.storage_state == MediaStorageState.PENDING)
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def mismatched_count(self) -> int:
        """How many objects were found to disagree with the row that owns them.

        Never zero by accident. Anything above zero is an object in the bucket
        that Wasla wrote a key for and did not write the contents of, and it
        needs a person.
        """
        statement = (
            select(func.count())
            .select_from(MessageMedia)
            .where(MessageMedia.storage_state == MediaStorageState.MISMATCHED)
        )
        return int((await self._session.execute(statement)).scalar_one())
