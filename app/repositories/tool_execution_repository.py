"""Durable records of what the tool executor did (TOOL-12).

Three writes and two reads, and the transaction boundary matters more than any
of them. A record of a *successful* tool call has to commit with the mutation it
describes - a row saying `succeeded` beside a rolled-back lead is worse than no
row at all - so nothing here commits. The executor stages the record in the
turn's own session, the handler's work goes into a savepoint inside that same
transaction, and the round boundary commits both together or neither.

The one exception is `record_terminal`, which the executor uses when the turn's
transaction has been lost. Then, and only then, the outcome is written through a
unit of work of its own, because a terminal outcome nobody can read is the thing
this table exists to stop.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement

from app.db.models.tool_execution import (
    ToolExecution,
    ToolExecutionReason,
    ToolExecutionState,
)
from app.repositories.base import TenantScopedRepository


class ToolExecutionRepository(TenantScopedRepository[ToolExecution]):
    """Tool execution records for one workspace."""

    model = ToolExecution

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ToolExecution.tenant_id == self._tenant_id

    def start(
        self,
        *,
        conversation_id: uuid.UUID,
        tool_name: str,
        round_number: int,
        call_ordinal: int,
        agent_turn_id: uuid.UUID | None = None,
        trigger_message_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        provider_call_id: str | None = None,
        argument_fields: list[str] | None = None,
        now: datetime | None = None,
    ) -> ToolExecution:
        """Stage the `REQUESTED` record for one call the provider asked for.

        Written before anything is checked, so a call refused by the very first
        gate still leaves the same evidence as one that ran. The tool name is
        whatever the model emitted - a name no deployment implements is exactly
        the request worth having a row for - truncated to the column so a model
        emitting a paragraph cannot fail the insert.
        """
        moment = now or datetime.now(UTC)
        execution = ToolExecution(
            tenant_id=self._tenant_id,
            agent_turn_id=agent_turn_id,
            trigger_message_id=trigger_message_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            tool_name=tool_name[:100],
            provider_call_id=provider_call_id[:128] if provider_call_id else None,
            round_number=round_number,
            call_ordinal=call_ordinal,
            state=ToolExecutionState.REQUESTED,
            requested_at=moment,
            argument_fields=argument_fields,
        )
        self._session.add(execution)
        return execution

    @staticmethod
    def authorize(execution: ToolExecution, *, now: datetime | None = None) -> None:
        """Every gate passed. The handler has not run yet."""
        execution.state = ToolExecutionState.AUTHORIZED
        execution.authorized_at = now or datetime.now(UTC)

    @staticmethod
    def begin(execution: ToolExecution, *, now: datetime | None = None) -> None:
        """The handler was entered."""
        execution.state = ToolExecutionState.STARTED
        execution.started_at = now or datetime.now(UTC)

    @staticmethod
    def settle(
        execution: ToolExecution,
        *,
        state: ToolExecutionState,
        reason: ToolExecutionReason | None = None,
        now: datetime | None = None,
    ) -> None:
        """Close the record out.

        `finished_at` is set here and nowhere else, so "terminal without a
        finish time" is a database invariant rather than a convention.
        """
        execution.state = state
        execution.reason_code = reason
        execution.finished_at = now or datetime.now(UTC)

    async def list_for_turn(self, agent_turn_id: uuid.UUID) -> Sequence[ToolExecution]:
        """Every call of one turn, in the order the provider asked for them."""
        return await self._all(
            self._select()
            .where(ToolExecution.agent_turn_id == agent_turn_id)
            .order_by(ToolExecution.round_number, ToolExecution.call_ordinal)
        )

    async def list_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 200,
    ) -> Sequence[ToolExecution]:
        """What agents have done in one conversation, newest last."""
        return await self._all(
            self._select()
            .where(ToolExecution.conversation_id == conversation_id)
            .order_by(ToolExecution.requested_at)
            .limit(limit)
        )


def terminal_values(
    *,
    state: ToolExecutionState,
    reason: ToolExecutionReason | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The column values a terminal outcome sets, for an UPDATE by id.

    Used by the executor's last-resort path, where the turn's transaction has
    been lost and the record has to be closed out through a clean unit of work
    that cannot hold the ORM object.
    """
    return {
        "state": state,
        "reason_code": reason,
        "finished_at": now or datetime.now(UTC),
    }


__all__ = ["ToolExecutionRepository", "terminal_values"]
