"""The team's read position, and what a member's reply does to it (UR, ADR-198).

Shared by the team, from the server: one member seeing a conversation makes it
read for every member, and the inbox of each of them follows the
`conversation.updated` frame (fields include `read`) the move emits.

- `mark_read` moves the position forward, never back. An older or equal message
  is a no-op that changes nothing, `read.by` and `read.at` included.
- `mark_unread` is the one way back: to just before the customer's newest
  message, so at least that message counts again.
- `after_member_reply` is what a person's send does beside the send itself:
  replying means having seen what came before, and the first member to reply to
  an unassigned conversation becomes its assignee - through the ordinary
  assignment, its audit entry and its frame. Opening or reading a conversation
  never assigns it.

Not audited: a read position is not a business action. Nothing here logs a
message's text.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models.conversation import Conversation, Message, MessageOrigin, MessageStatus
from app.db.models.user import User
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.read_state_repository import (
    EMPTY_READ_STATE,
    ReadState,
    ReadStateRepository,
)
from app.services.inbox_service import STALE_ASSIGNMENT, InboxService

logger = get_logger(__name__)


class ReadStateService:
    """Read positions of one workspace's conversations."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._reads = ReadStateRepository(session, tenant_id=tenant_id)
        self._session = session
        self._tenant_id = tenant_id

    async def states(self, conversation_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, ReadState]:
        """Unread count, preview and marker of each conversation, in one query."""
        return await self._reads.states(conversation_ids)

    async def state(self, conversation_id: uuid.UUID) -> ReadState:
        return (await self._reads.states([conversation_id])).get(conversation_id, EMPTY_READ_STATE)

    async def summary(self) -> tuple[int, int]:
        """Unread customer messages and the conversations holding them, not closed."""
        return await self._reads.summary()

    async def mark_read(
        self, *, conversation_id: uuid.UUID, up_to_message_id: uuid.UUID, actor: User
    ) -> ReadState:
        """Move the team's read position to `up_to_message_id`, if that is forward."""
        conversation = await self._conversations.lock_by_id(conversation_id)
        target = await self._reads.position(
            conversation_id=conversation.id, message_id=up_to_message_id
        )
        if target is None:
            # A message of another conversation or workspace: no oracle.
            raise NotFoundError()
        await self._advance(conversation, message_id=up_to_message_id, position=target, actor=actor)
        return await self.state(conversation.id)

    async def mark_unread(self, *, conversation_id: uuid.UUID, actor: User) -> ReadState:
        """Move the position back to just before the customer's newest message."""
        conversation = await self._conversations.lock_by_id(conversation_id)
        newest = await self._reads.newest_inbound(conversation.id)
        if newest is None:
            return await self.state(conversation.id)
        before = await self._reads.newest_before(conversation_id=conversation.id, sequence=newest)
        conversation.last_read_message_id = None if before is None else before[0]
        conversation.last_read_by_user_id = actor.id
        conversation.last_read_at = datetime.now(UTC)
        await self._session.flush()
        logger.info(
            "conversation.marked_unread",
            extra={"event": "conversation.marked_unread", "conversation_id": str(conversation.id)},
        )
        return await self.state(conversation.id)

    async def after_member_reply(
        self, *, message: Message, actor: User, inbox: InboxService, replayed: bool
    ) -> None:
        """What a member's own send does to the conversation (UR-06).

        Only for a person's send that was recorded as an attempt: never the AI's,
        a campaign's or a follow-up's, never a send refused before it was written,
        and never an idempotent replay of a send already made.
        """
        if replayed or message.origin is not MessageOrigin.HUMAN:
            return
        if message.status is MessageStatus.FAILED:
            return
        conversation = await self._conversations.lock_by_id(message.conversation_id)
        before = await self._reads.newest_before(
            conversation_id=conversation.id, sequence=message.sequence
        )
        if before is not None:
            await self._advance(conversation, message_id=before[0], position=before[1], actor=actor)
        if conversation.assigned_to_id is not None:
            return
        try:
            await inbox.assign(
                conversation_id=conversation.id,
                assigned_to_id=actor.id,
                expected_assigned_to_id=None,
                actor=actor,
            )
        except ConflictError as error:
            # Somebody assigned it first: theirs stays, and this send stands.
            if error.error_code != STALE_ASSIGNMENT:
                raise

    async def _advance(
        self, conversation: Conversation, *, message_id: uuid.UUID, position: int, actor: User
    ) -> None:
        current = None
        if conversation.last_read_message_id is not None:
            current = await self._reads.position(
                conversation_id=conversation.id, message_id=conversation.last_read_message_id
            )
        if current is not None and position <= current:
            return
        conversation.last_read_message_id = message_id
        conversation.last_read_by_user_id = actor.id
        conversation.last_read_at = datetime.now(UTC)
        await self._session.flush()


__all__ = ["ReadStateService"]
