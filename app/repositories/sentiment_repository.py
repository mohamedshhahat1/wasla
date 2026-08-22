"""Data access for sentiment readings."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement

from app.db.models.sentiment import MessageSentiment, SentimentLabel
from app.repositories.base import TenantScopedRepository


class SentimentRepository(TenantScopedRepository[MessageSentiment]):
    """Readings taken in one workspace."""

    model = MessageSentiment

    def _tenant_filter(self) -> ColumnElement[bool]:
        return MessageSentiment.tenant_id == self.tenant_id

    async def get_for_message(self, message_id: uuid.UUID) -> MessageSentiment | None:
        return await self._first(self._select().where(MessageSentiment.message_id == message_id))

    async def list_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> list[MessageSentiment]:
        """Readings for one conversation, newest first."""
        return await self._all(
            self._select()
            .where(MessageSentiment.conversation_id == conversation_id)
            .order_by(MessageSentiment.created_at.desc(), MessageSentiment.id.desc())
            .limit(limit)
        )

    async def record(
        self,
        *,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        label: SentimentLabel,
        score: float,
        intent: str | None,
        confidence: float,
        escalated: bool,
        model: str | None = None,
    ) -> tuple[MessageSentiment, bool]:
        """Store a reading. Returns the row and whether it is new.

        A message already carrying one is returned unchanged rather than
        re-read. That is what stops a retried agent job from paying for a second
        inference, and the unique constraint would refuse the row anyway -
        checking first turns a crash into a no-op.
        """
        existing = await self.get_for_message(message_id)
        if existing is not None:
            return existing, False

        reading = MessageSentiment(
            tenant_id=self.tenant_id,
            message_id=message_id,
            conversation_id=conversation_id,
            label=label,
            score=score,
            intent=intent,
            confidence=confidence,
            escalated=escalated,
            model=model,
        )
        self.add(reading)
        return reading, True
