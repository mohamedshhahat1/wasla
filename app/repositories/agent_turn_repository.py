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
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.logging import get_logger
from app.db.models.agent_turn import AgentTurn, AgentTurnState, TurnOutcome
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = get_logger(__name__)

#: How long a `CLAIMED` turn stays somebody's before another attempt may adopt
#: it. Matches the queue's own visibility default rather than inventing a second
#: number: the window this covers is the one between claiming a turn and
#: engaging a provider, which is a handful of database round trips, and a worker
#: that has not crossed it in two minutes is a worker that is not going to.
DEFAULT_CLAIM_SECONDS = 120.0

#: How long a turn may stay `ENGAGED` before it counts as stranded (AI-09). The
#: longest a healthy turn can take is a classification and three rounds, each up
#: to three attempts of sixty seconds - twelve minutes - so fifteen is past
#: anything a working turn does and short enough to page on while the customer
#: still remembers writing in.
STRANDED_TURN_AFTER: Final = timedelta(minutes=15)

#: Who holds a turn the media release has owed and no agent worker has yet
#: taken up. Not a worker id - nothing runs under it - but the mark that makes
#: the obligation findable: the first agent worker to reach the turn adopts it
#: and writes its own id here, so a turn still carrying this one is a turn whose
#: job never arrived.
MEDIA_RELEASE_HOLDER: Final = "media-release"


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

    async def owe(
        self,
        *,
        conversation_id: uuid.UUID,
        trigger_message_id: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        """Record that this turn is owed, in the transaction that decided it.

        The media release decides a conversation is answerable in the same
        transaction that makes its last file terminal, and until this existed
        the only record of that decision was a job put on Redis after the
        commit. A refused enqueue lost the turn: the file was final, nothing
        was unresolved, and nothing anywhere said a reply was owed.

        Staged as the turn itself rather than as a second ledger, because the
        turn already carries the identity that makes it happen once. `CLAIMED`
        with a lease that has already lapsed is exactly "nothing has run this
        and anybody may": the agent worker's `claim` finds the row, adopts it
        through its existing expired-lease branch, and proceeds as it would
        have on a fresh insert. `OwedReleaseSweep` finds the ones nobody came
        for.

        Returns whether this call recorded the obligation. A turn already there
        for this message is left untouched: it is either owed already or
        somebody's, and neither is improved by writing over it.
        """
        moment = now or datetime.now(UTC)
        statement = (
            insert(AgentTurn)
            .values(
                id=uuid.uuid4(),
                tenant_id=self._tenant_id,
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                state=AgentTurnState.CLAIMED,
                claimed_by=MEDIA_RELEASE_HOLDER,
                claim_expires_at=moment,
            )
            .on_conflict_do_nothing(
                constraint="uq_agent_turns_tenant_id_trigger_message_id",
            )
            .returning(AgentTurn.id)
        )
        recorded: uuid.UUID | None = (await self._session.execute(statement)).scalar_one_or_none()
        return recorded is not None

    async def id_for(self, *, trigger_message_id: uuid.UUID) -> uuid.UUID | None:
        """This turn's durable id, for the records its tools will write.

        A separate read rather than a value threaded out of `claim`, because a
        turn may also be *adopted* - the claim's second branch updates a row it
        did not insert - and both paths need the same answer.
        """
        found: uuid.UUID | None = await self._session.scalar(
            select(AgentTurn.id).where(
                AgentTurn.tenant_id == self._tenant_id,
                AgentTurn.trigger_message_id == trigger_message_id,
            )
        )
        return found

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

    async def complete(
        self,
        *,
        trigger_message_id: uuid.UUID,
        now: datetime | None = None,
        outcome: TurnOutcome | None = None,
        provider_response_id: str | None = None,
    ) -> bool:
        """Record that this turn ran to its end, and how.

        A reply sent, a handoff, a suppression, a refusal: all are the turn
        finishing, and none of them is owed anything further. Refusing a turn
        that never engaged would leave a claim behind for a turn that is over.

        `outcome` says which ending it was, so none of them is a silence nobody
        can explain afterwards. The worker always passes one.
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
                outcome=outcome,
                provider_response_id=provider_response_id,
            )
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return bool(result.rowcount)

    async def get(self, *, trigger_message_id: uuid.UUID) -> AgentTurn | None:
        """The turn answering this message, if one has been claimed."""
        return await self._first(
            self._select().where(AgentTurn.trigger_message_id == trigger_message_id)
        )


class EngagedTurnSweep(BaseRepository[AgentTurn]):
    """Turns that engaged a provider and never finished, across the deployment.

    Unscoped, like `UnresolvedOutboundDirectory`, and for the same reason: a
    backlog of stranded turns is a platform-wide condition, and the only caller
    is the metrics exposition, which counts rather than answers a person. Which
    workspaces they belong to is a question for the runbook's query.

    Not a resolver. An `ENGAGED` turn may already have put a reply on a
    customer's phone, which is why nothing retries it; this exists so that a
    person is told there are some to look at (AI-09).
    """

    model = AgentTurn

    async def backlog(self, *, older_than: datetime) -> tuple[int, float]:
        """How many engaged turns are older than `older_than`, and the oldest's age."""
        rows = await self.session.execute(
            select(func.count(AgentTurn.id), func.min(AgentTurn.engaged_at)).where(
                AgentTurn.state == AgentTurnState.ENGAGED,
                AgentTurn.engaged_at < older_than,
            )
        )
        count, oldest = rows.one()
        if not count or oldest is None:
            return 0, 0.0
        return int(count), max((datetime.now(UTC) - oldest).total_seconds(), 0.0)


class OwedReleaseSweep(BaseRepository[AgentTurn]):
    """Turns the media release owed that no agent worker has taken up.

    Unscoped, like `EngagedTurnSweep`: the caller is the media recovery loop
    and the metrics exposition, both platform-wide. What makes a row one of
    these is `MEDIA_RELEASE_HOLDER` still in `claimed_by` - the first agent
    worker to reach the turn replaces it - so the set is exactly the owed
    turns whose job was refused by Redis, lost with a crashed process, or is
    still waiting behind a backlog.

    `claim_expires_at` doubles as "last published": the lease on these rows
    has lapsed from the moment they were written, so the column carries no
    other meaning, and stamping it on each publish is what keeps one
    obligation from being published every pass while its job is merely
    queued.
    """

    model = AgentTurn

    def _owed(self) -> ColumnElement[bool]:
        return (AgentTurn.state == AgentTurnState.CLAIMED) & (
            AgentTurn.claimed_by == MEDIA_RELEASE_HOLDER
        )

    async def claim_owed(
        self, *, published_before: datetime, now: datetime, limit: int = 100
    ) -> list[AgentTurn]:
        """Take the owed turns not published since `published_before`, and stamp them.

        `SKIP LOCKED`, and stamped in the caller's transaction, so two sweeps
        divide the rows between them and a row one of them has just published
        is not old enough for the other. The caller commits, then publishes -
        a publish that fails, or a process that dies before it, leaves the row
        stamped and owed, and the pass after the horizon tries again.
        """
        rows = await self._all(
            self._select()
            .where(self._owed(), AgentTurn.claim_expires_at < published_before)
            .order_by(AgentTurn.claim_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            row.claim_expires_at = now
        await self._session.flush()
        return rows

    async def backlog(self, *, older_than: datetime, now: datetime) -> tuple[int, float]:
        """How many owed turns were released before `older_than`, and the oldest's age.

        Measured from when the release was owed, not from the last publish:
        a sweep republishing into a Redis that keeps refusing re-stamps every
        pass, and the reading must keep climbing through exactly that.
        """
        rows = await self._session.execute(
            select(func.count(AgentTurn.id), func.min(AgentTurn.created_at)).where(
                self._owed(), AgentTurn.created_at < older_than
            )
        )
        count, oldest = rows.one()
        if not count or oldest is None:
            return 0, 0.0
        return int(count), max((now - oldest).total_seconds(), 0.0)


__all__ = [
    "DEFAULT_CLAIM_SECONDS",
    "MEDIA_RELEASE_HOLDER",
    "STRANDED_TURN_AFTER",
    "AgentTurnRepository",
    "EngagedTurnSweep",
    "OwedReleaseSweep",
]
