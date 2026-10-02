"""Settling an AI turn's hold on the workspace's allowance: charge it or release it.

ENT-02 and ENT-03 (ADR-131). A turn takes a **hold** on one unit of the
workspace's AI allowance when it engages, under the workspace's advisory lock
(`EntitlementService.hold_ai_turn`), and the hold is spoken for until the turn
settles here:

* a usable outcome - reply text, or a handoff the agent's own tool executed -
  **charges** the turn: one `ai_turn` usage event, stamped with the moment the
  hold was taken so it counts in the usage cycle that allowed it, and with the
  conversation's channel and connection as reporting dimensions (ENT-01);
* anything else **releases** the hold, and nothing is recorded.

Settling is one short transaction on the turn's own row - locked, decided,
written - so a retried settle, a settle racing the expired-hold sweep or a
settle of a turn that never held changes nothing it should not. The unique
index on `usage_events (tenant_id, agent_turn_id)` is the backstop: a second
charge for one turn cannot be written, whoever tries.

No advisory lock here. The lock orders *decisions to hold*; a settle only ever
turns a hold into a charge of the same size, or gives it back, so the count the
next hold decision reads is never larger after a settle than before it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.agent_turn import AgentTurn, AITurnChargeState, AITurnReleaseReason
from app.db.models.conversation import Conversation
from app.db.models.usage import UsageEvent, UsageEventType, unit_for

logger = get_logger(__name__)


class SettleResult(StrEnum):
    """What one settle did. Also the outcome label of the charge metric."""

    #: The hold became a charge: one `ai_turn` event recorded.
    CHARGED = "charged"
    #: The sweep had released the hold as expired, and the turn produced a
    #: usable outcome after all: charged anyway, because usage that happened is
    #: never refused afterwards. A run of these means the TTL is too short.
    LATE_CHARGE = "late_charge"
    #: The hold was given back; nothing recorded.
    RELEASED = "released"
    #: Nothing to do: already settled, or the turn never held - a turn engaged
    #: before ADR-131, or one whose conversation was deleted mid-turn.
    UNCHANGED = "unchanged"


class AITurnCharge:
    """Charges or releases the holds of one workspace's AI turns."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    async def settle(
        self,
        *,
        agent_turn_id: uuid.UUID,
        chargeable: bool,
        reason: AITurnReleaseReason = AITurnReleaseReason.NOT_CHARGEABLE,
    ) -> SettleResult:
        """Charge the turn's hold if `chargeable`, release it otherwise; idempotent.

        Staged in the caller's transaction, which commits it. The turn's row is
        locked for that transaction: a concurrent settle waits and then finds
        the hold already settled, and the sweep skips it.
        """
        row = (
            await self._session.execute(
                select(
                    AgentTurn.charge_state,
                    AgentTurn.charge_release_reason,
                    AgentTurn.held_at,
                    AgentTurn.conversation_id,
                )
                .where(AgentTurn.tenant_id == self._tenant_id, AgentTurn.id == agent_turn_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row.charge_state is None or row.held_at is None:
            return SettleResult.UNCHANGED
        now = self._clock()
        expired = (
            row.charge_state is AITurnChargeState.RELEASED
            and row.charge_release_reason is AITurnReleaseReason.HOLD_EXPIRED
        )

        this_turn = (
            AgentTurn.tenant_id == self._tenant_id,
            AgentTurn.id == agent_turn_id,
            AgentTurn.charge_state == row.charge_state,
        )

        if not chargeable:
            if row.charge_state is not AITurnChargeState.HELD:
                return SettleResult.UNCHANGED
            await self._session.execute(
                update(AgentTurn)
                .where(*this_turn)
                .values(
                    charge_state=AITurnChargeState.RELEASED,
                    released_at=now,
                    charge_release_reason=reason,
                )
            )
            self._log("ai.turn_hold_released", agent_turn_id, reason=reason.value)
            return SettleResult.RELEASED

        if row.charge_state is not AITurnChargeState.HELD and not expired:
            # Charged already, or released by the turn's own decision: a settle
            # never reverses what the turn concluded.
            return SettleResult.UNCHANGED

        # A late charge keeps the sweep's release beside it: the turn's history
        # says it outlived its TTL and was charged anyway.
        await self._session.execute(
            update(AgentTurn)
            .where(*this_turn)
            .values(charge_state=AITurnChargeState.CHARGED, charged_at=now)
        )
        recorded = await self._record_charge(
            agent_turn_id=agent_turn_id,
            conversation_id=row.conversation_id,
            occurred_at=row.held_at,
        )
        if not recorded:  # pragma: no cover - the row lock above makes this unreachable
            logger.error(
                "ai.turn_charge_duplicate",
                extra={
                    "event": "ai.turn_charge_duplicate",
                    "tenant_id": str(self._tenant_id),
                    "agent_turn_id": str(agent_turn_id),
                },
            )
        result = SettleResult.LATE_CHARGE if expired else SettleResult.CHARGED
        self._log(f"ai.turn_{result.value}", agent_turn_id)
        return result

    async def _record_charge(
        self,
        *,
        agent_turn_id: uuid.UUID,
        conversation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> bool:
        """Write the turn's one `ai_turn` event; False if one is already there.

        `occurred_at` is when the hold was taken, so the charge lands in the
        usage cycle whose allowance admitted the turn - a turn engaged at
        23:59 on the last day and finished after midnight is the old cycle's.
        The channel and connection are the conversation's, read here so no
        caller can stamp a turn with a channel it was not on (ENT-01).
        """
        where = (
            await self._session.execute(
                select(Conversation.channel, Conversation.account_id).where(
                    Conversation.tenant_id == self._tenant_id,
                    Conversation.id == conversation_id,
                )
            )
        ).one_or_none()
        channel, connection_id = (where.channel, where.account_id) if where else (None, None)
        statement = (
            insert(UsageEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=self._tenant_id,
                event_type=UsageEventType.AI_TURN,
                quantity=1,
                unit=unit_for(UsageEventType.AI_TURN),
                occurred_at=occurred_at,
                meta={"conversation_id": str(conversation_id)},
                channel=channel,
                connection_id=connection_id,
                agent_turn_id=agent_turn_id,
            )
            .on_conflict_do_nothing(
                index_elements=[UsageEvent.tenant_id, UsageEvent.agent_turn_id],
                index_where=text("agent_turn_id IS NOT NULL"),
            )
            .returning(UsageEvent.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    def _log(self, event: str, agent_turn_id: uuid.UUID, **extra: str) -> None:
        logger.info(
            event,
            extra={
                "event": event,
                "tenant_id": str(self._tenant_id),
                "agent_turn_id": str(agent_turn_id),
                **extra,
            },
        )


__all__ = ["AITurnCharge", "SettleResult"]
