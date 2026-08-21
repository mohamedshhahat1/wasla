"""Agent-facing conversation operations.

The read side of the inbox plus the state a human controls: who owns a
conversation, whether the AI answers it, and whether it is still open.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
)
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.membership_repository import MembershipRepository

logger = get_logger(__name__)


class InboxService:
    """Conversation operations for one workspace."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._memberships = MembershipRepository(session, tenant_id=tenant_id)

    async def list_conversations(self, *, limit: int = 50) -> list[Conversation]:
        """Everything not closed, most recently active first."""
        return await self._conversations.list_open(limit=limit)

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        return await self._conversations.require_by_id(conversation_id)

    async def list_messages(
        self,
        *,
        conversation_id: uuid.UUID,
        limit: int = 50,
    ) -> list[Message]:
        # Resolved first so another workspace's id answers not-found instead of
        # an empty list, which would leak that the conversation exists.
        await self._conversations.require_by_id(conversation_id)
        return await self._messages.list_for_conversation(
            conversation_id=conversation_id,
            limit=limit,
        )

    async def set_mode(
        self,
        *,
        conversation_id: uuid.UUID,
        mode: ConversationMode,
        handoff_reason: str | None = None,
    ) -> Conversation:
        """Hand a conversation to a human, or give it back to the AI.

        The reason belongs to the handoff, so returning to AI clears it rather
        than leaving a stale explanation attached.
        """
        conversation = await self._conversations.require_by_id(conversation_id)
        conversation.mode = mode
        conversation.handoff_reason = handoff_reason if mode is ConversationMode.HUMAN else None
        logger.info(
            "conversation.mode_changed",
            extra={"conversation_id": str(conversation_id), "mode": mode.value},
        )
        return conversation

    async def assign(
        self,
        *,
        conversation_id: uuid.UUID,
        assigned_to_id: uuid.UUID | None,
    ) -> Conversation:
        """Assign to a member of this workspace, or clear the assignment.

        Membership is verified rather than assumed: the id arrives in a request
        body, and an unchecked one would attach a workspace's conversation to
        someone outside it.
        """
        conversation = await self._conversations.require_by_id(conversation_id)
        if assigned_to_id is not None:
            await self._memberships.require_for_user(assigned_to_id)
        conversation.assigned_to_id = assigned_to_id
        return conversation

    async def close(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._conversations.require_by_id(conversation_id)
        conversation.status = ConversationStatus.CLOSED
        return conversation

    async def reopen(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._conversations.require_by_id(conversation_id)
        conversation.status = ConversationStatus.OPEN
        return conversation
