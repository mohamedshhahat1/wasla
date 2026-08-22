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

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation
from app.db.models.media import (
    UNRESOLVED_MEDIA_STATUSES,
    MediaStatus,
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
