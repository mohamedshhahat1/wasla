"""Claiming, engaging and finishing one logical agent turn.

The whole of WQ-01's first line of defence lives here, and it is three
statements. What makes it correct is not their complexity but where their
transactions end: none of them is held open across an inference, a tool call or
a WhatsApp send, because a row lock across a provider call is the thing ADR-080
exists to keep out of this codebase.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, update
from sqlalchemy.dialects.postgresql import insert

from app.core.logging import get_logger
from app.db.models.agent_turn import AgentTurn, AgentTurnState
from app.repositories.base import TenantScopedRepository

logger = get_logger(__name__)

#: How long a `CLAIMED` turn stays somebody's before another attempt may adopt
#: it. Matches the queue's own visibility default rather than inventing a second
#: number: the window this covers is the one between claiming a turn and
#: engaging a provider, which is a handful of database round trips, and a worker
#: that has not crossed it in two minutes is a worker that is not going to.
DEFAULT_CLAIM_SECONDS = 120.0


class AgentTurnRepository(TenantScopedRepository[AgentTurn]):
    """The durable identity of one agent turn, within one workspace."""

    model = AgentTurn

    def _tenant_filter(self) -> ColumnElement[bool]:
        return AgentTurn.tenant_id == self._tenant_id

    async def claim(
        self,
        *,
        conversation_id: uuid.UUID,
        trigger_message_id: uuid.UUID,
        worker_id: str | None = None,
        now: datetime | None = None,
        claim_seconds: float = DEFAULT_CLAIM_SECONDS,
    ) -> bool:
        """Take ownership of this turn, or answer False because somebody else has.

        One statement, and it has to be one statement. A read followed by an
        insert would let two workers both read "nothing there" and both insert,
        which is precisely the race this exists to decide; the unique index is
        the arbiter, and `ON CONFLICT` is how the loser is told without raising.

        The conflict branch is the interesting half. A row already there means
        one of three things, and only one of them is a duplicate:

        * `ENGAGED` or `COMPLETED` - a provider may already have been called and
          a customer may already have a reply. Refused, permanently, with no
          lease and no way back. This is the same judgement `ReservationStage`
          makes on the queue (ADR-074), made about the business fact rather than
          about the envelope.
        * `CLAIMED` and still leased - another attempt is running this turn
          right now. Refused.
        * `CLAIMED` and the lease has gone - the attempt that held it stopped
          without engaging anything, so nothing has left the process and
          repeating the work is free. Adopted, with a fresh lease.

        Returns whether this caller may proceed. The caller commits; this only
        stages.
        """
        moment = now or datetime.now(UTC)
        expires = moment + timedelta(seconds=claim_seconds)

        statement = (
            insert(AgentTurn)
            .values(
                id=uuid.uuid4(),
                tenant_id=self._tenant_id,
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                state=AgentTurnState.CLAIMED,
                claimed_by=worker_id,
                claim_expires_at=expires,
            )
            .on_conflict_do_nothing(
                constraint="uq_agent_turns_tenant_id_trigger_message_id",
            )
            .returning(AgentTurn.id)
        )
        won: uuid.UUID | None = (await self._session.execute(statement)).scalar_one_or_none()
        if won is not None:
            return True

        return await self._adopt(
            trigger_message_id=trigger_message_id,
            worker_id=worker_id,
            moment=moment,
            expires=expires,
        )

    async def _adopt(
        self,
        *,
        trigger_message_id: uuid.UUID,
        worker_id: str | None,
        moment: datetime,
        expires: datetime,
    ) -> bool:
        """Take over a claim whose lease has gone, or answer False.

        Conditional in the `WHERE` clause rather than in Python, so two workers
        arriving at an expired claim together do not both decide they may have
        it: the update either matched a row or it did not, and only one of them
        can be told it did.
        """
        statement = (
            update(AgentTurn)
            .where(
                AgentTurn.tenant_id == self._tenant_id,
                AgentTurn.trigger_message_id == trigger_message_id,
                AgentTurn.state == AgentTurnState.CLAIMED,
                AgentTurn.claim_expires_at.is_not(None),
                AgentTurn.claim_expires_at < moment,
            )
            .values(claimed_by=worker_id, claim_expires_at=expires)
        )
        # `CursorResult.rowcount`, which `Result` does not declare - an ORM
        # UPDATE always returns the cursor variant, and how many rows it
        # matched is the answer. The same narrowing `identity_repository`
        # makes for the same reason.
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        adopted = bool(result.rowcount)
        if not adopted:
            logger.info(
                "agent.turn_already_owned",
                extra={
                    "event": "agent.turn_already_owned",
                    "tenant_id": str(self._tenant_id),
                    "trigger_message_id": str(trigger_message_id),
                },
            )
        return adopted

    async def engage(self, *, trigger_message_id: uuid.UUID, now: datetime | None = None) -> bool:
        """Record that this turn is about to call somebody else's API.

        The point of no return, and the reason the lease is cleared rather than
        extended: an engaged turn is never adoptable again, by anybody, so a
        lease on it would be one that must never be allowed to expire - and a
        deadline nobody may act on is worse than no deadline at all.

        Conditional on the turn still being `CLAIMED`, so this cannot drag a
        turn somebody else has already finished back into flight.
        """
        moment = now or datetime.now(UTC)
        statement = (
            update(AgentTurn)
            .where(
                AgentTurn.tenant_id == self._tenant_id,
                AgentTurn.trigger_message_id == trigger_message_id,
                AgentTurn.state == AgentTurnState.CLAIMED,
            )
            .values(
                state=AgentTurnState.ENGAGED,
                engaged_at=moment,
                claim_expires_at=None,
            )
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return bool(result.rowcount)

    async def complete(self, *, trigger_message_id: uuid.UUID, now: datetime | None = None) -> bool:
        """Record that this turn ran to its end, whatever that end was.

        A reply sent, a handoff, or a deliberate silence: all three are the turn
        finishing, and none of them is owed anything further. Refusing a turn
        that never engaged would leave a claim behind for a turn that is over.
        """
        moment = now or datetime.now(UTC)
        statement = (
            update(AgentTurn)
            .where(
                AgentTurn.tenant_id == self._tenant_id,
                AgentTurn.trigger_message_id == trigger_message_id,
                AgentTurn.state != AgentTurnState.COMPLETED,
            )
            .values(
                state=AgentTurnState.COMPLETED,
                completed_at=moment,
                claim_expires_at=None,
            )
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return bool(result.rowcount)

    async def get(self, *, trigger_message_id: uuid.UUID) -> AgentTurn | None:
        """The turn answering this message, if one has been claimed."""
        return await self._first(
            self._select().where(AgentTurn.trigger_message_id == trigger_message_id)
        )


__all__ = ["DEFAULT_CLAIM_SECONDS", "AgentTurnRepository"]
