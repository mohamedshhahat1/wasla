"""Data access for sentiment readings."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement
from sqlalchemy.dialects.postgresql import insert

from app.core.exceptions import ConflictError
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
        """Store a reading, atomically. Returns the row and whether this call wrote it.

        One statement, and it has to be one statement (AI-04). This used to read
        and then insert, which is correct inside one transaction and wrong across
        two: two turns on one conversation both read "no reading", both
        inserted, and the second commit raised `UniqueViolation` *after* its turn
        had engaged the provider - dead-lettering the job and stranding the
        customer's message for ever. The unique constraint is the arbiter, and
        `ON CONFLICT` is how the loser is told rather than raised at: its insert
        waits for the winner, does nothing, and reads the winner's row back.

        The same shape `AgentTurnRepository.claim` uses a few files away, for
        the same reason.
        """
        statement = (
            insert(MessageSentiment)
            .values(
                id=uuid.uuid4(),
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
            .on_conflict_do_nothing(constraint="uq_message_sentiments_message_id")
            .returning(MessageSentiment.id)
        )
        written: uuid.UUID | None = (await self.session.execute(statement)).scalar_one_or_none()
        if written is not None:
            row = await self._first(self._select().where(MessageSentiment.id == written))
        else:
            row = await self.get_for_message(message_id)
        if row is None:
            # The conflicting reading is outside this workspace, which a message
            # belonging to this workspace cannot produce. Refused rather than
            # answered with a row the caller may not see.
            raise ConflictError("That message already carries a reading.")
        return row, written is not None
