"""Two turns of one conversation racing the same tool (TOOL-02).

A customer writing twice in a few seconds is ordinary WhatsApp behaviour, and
this product deliberately does not coalesce a burst into one turn (AI-08) - so
two turns of one conversation running at once is supported behaviour, not an
exotic race. Both can reach `record_lead_details` or `schedule_follow_up`, and
the database refuses the second: `uq_leads_active_contact` and
`uq_follow_ups_pending_conversation` each allow one row.

The refusal was correct and its handling was not. The loser's `IntegrityError`
escaped the tool, aborted the turn's whole transaction, and left the turn
stranded `ENGAGED`: the second customer message answered by silence, nothing
handing the conversation to a person, and the workspace charged for the turn.

The race is *proved*, not assumed, and in two shapes because they prove
different halves.

The first pair runs two real turns of one conversation at once, held at the
classifier until both have arrived, and asserts what the customer gets: two
answers, one lead or one pending nudge, and no turn left stranded.

The second pair proves the losing path itself. Two turns released together
usually collide, and "usually" is not a property - so the competitor there is a
second connection holding an uncommitted conflicting insert, which the turn's
own write then blocks behind. A real lock wait is observed in `pg_stat_activity`
before the competitor commits, so the turn provably *lost* the race rather than
being lucky, and the assertion is that losing costs the call and not the turn.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Executable, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.registry import RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL
from app.db.models.conversation import Conversation
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import Lead, LeadSource
from app.db.models.tool_execution import ToolExecutionState
from tests.integration.ai_harness import (
    FakeProviders,
    JsonObject,
    TurnRunner,
    Workspace,
    scripted,
    text_response,
    tool_call_response,
    wait_for_lock_waiter,
)

pytestmark = pytest.mark.integration

ANSWER = "Noted."


async def _both_turns(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    *,
    tool: str,
    arguments: JsonObject,
) -> tuple[int, int]:
    """Run two turns of one conversation at once, through the same tool.

    Returns how many turns reached round one together and how many workers ran,
    so the caller can assert the race happened before reading what it produced.
    """
    workspace = await ai_turns.workspace(grants=[tool])
    conversation_id, ids = await ai_turns.write(workspace, ["first", "second"])

    arrived = asyncio.Event()
    waiting = 0
    barrier = asyncio.Event()

    async def hold() -> None:
        """Keep both turns at the classifier until both have got there."""
        nonlocal waiting
        waiting += 1
        if waiting >= 2:
            arrived.set()
        await barrier.wait()

    ai_providers.sentiment_gate = hold
    ai_providers.agent = scripted(
        tool_call_response(tool, arguments, text="One moment."),
        text_response(ANSWER),
    )

    for message_id in ids:
        await ai_turns.enqueue(workspace, conversation_id, message_id)

    race = asyncio.create_task(ai_turns.race(2))
    try:
        await asyncio.wait_for(arrived.wait(), timeout=20)
        both_in_round_one = waiting
        barrier.set()
        consumed = await asyncio.wait_for(race, timeout=60)
    finally:
        barrier.set()
        ai_providers.sentiment_gate = None

    ai_turns.raced_workspace = workspace  # type: ignore[attr-defined]
    ai_turns.raced_conversation = conversation_id  # type: ignore[attr-defined]
    return both_in_round_one, consumed


async def test_two_turns_racing_the_lead_tool_both_answer_the_customer(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    both, consumed = await _both_turns(
        ai_turns,
        ai_providers,
        tool=RECORD_LEAD_TOOL,
        arguments={"name": "Ahmed", "interest": "finishing"},
    )
    workspace = ai_turns.raced_workspace  # type: ignore[attr-defined]

    assert both == 2, "the two turns never overlapped"
    assert consumed == 2

    # The database's rule still holds: one open lead per contact.
    assert len(await ai_turns.leads(workspace.tenant_id)) == 1
    # And neither customer message was answered by silence, which is the finding.
    assert sorted(await ai_turns.turn_states(workspace.tenant_id)) == ["completed", "completed"]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied", "replied"]
    assert len(await ai_turns.outbound(workspace.tenant_id)) == 2

    states = {row[1] for row in await ai_turns.execution_outcomes(workspace.tenant_id)}
    assert states == {ToolExecutionState.SUCCEEDED.value}


async def test_two_turns_racing_the_follow_up_tool_both_answer_the_customer(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    both, consumed = await _both_turns(
        ai_turns,
        ai_providers,
        tool=SCHEDULE_FOLLOW_UP_TOOL,
        arguments={"delay_minutes": 60, "message": "Still interested?"},
    )
    workspace = ai_turns.raced_workspace  # type: ignore[attr-defined]

    assert both == 2, "the two turns never overlapped"
    assert consumed == 2

    pending = [
        row
        for row in await ai_turns.follow_ups(workspace.tenant_id)
        if row.status is FollowUpStatus.PENDING
    ]
    assert len(pending) == 1, "the one-pending-nudge rule did not hold"
    assert sorted(await ai_turns.turn_states(workspace.tenant_id)) == ["completed", "completed"]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied", "replied"]
    assert len(await ai_turns.outbound(workspace.tenant_id)) == 2

    states = {row[1] for row in await ai_turns.execution_outcomes(workspace.tenant_id)}
    assert states == {ToolExecutionState.SUCCEEDED.value}


# --------------------------------------------------- the losing side, proved


@asynccontextmanager
async def _uncommitted(url: str, statement: Executable) -> AsyncIterator[AsyncConnection]:
    """Hold a conflicting row uncommitted, on a connection of its own.

    This is the other turn, reduced to the only part of it that matters: a write
    that has taken the unique index's entry and not yet committed. Anything else
    attempting the same key blocks behind it for as long as this stays open,
    which is what turns "they probably collided" into "it provably lost".
    """
    engine = create_async_engine(url, poolclass=NullPool)
    connection = await engine.connect()
    try:
        await connection.execute(statement)
        yield connection
    finally:
        await connection.close()
        await engine.dispose()


async def _contact_of(ai_turns: TurnRunner, conversation_id: uuid.UUID) -> uuid.UUID:
    async with ai_turns.database.session() as session:
        found = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation_id)
        )
    assert found is not None
    return found


async def _losing_turn(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    *,
    workspace: Workspace,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    statement: Executable,
) -> None:
    """Run one turn whose tool write is guaranteed to lose a race, and let it finish."""
    async with _uncommitted(ai_turns.url, statement) as competitor:
        turn = asyncio.create_task(ai_turns.answer(workspace, conversation_id, message_id))
        try:
            # Non-vacuity, and the whole point of this construction: the turn's
            # own insert is *blocked* behind the competitor's before anything is
            # read back.
            await wait_for_lock_waiter(ai_turns.database.engine, within=30.0)
        finally:
            await competitor.commit()
        await asyncio.wait_for(turn, timeout=60)


async def test_a_lead_write_that_loses_its_race_costs_the_call_and_not_the_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    contact_id = await _contact_of(ai_turns, conversation_id)
    ai_providers.agent = scripted(
        tool_call_response(RECORD_LEAD_TOOL, {"name": "Ahmed", "interest": "finishing"}),
        text_response(ANSWER),
    )

    await _losing_turn(
        ai_turns,
        ai_providers,
        workspace=workspace,
        conversation_id=conversation_id,
        message_id=ids[0],
        statement=insert(Lead).values(
            id=uuid.uuid4(),
            tenant_id=workspace.tenant_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            source=LeadSource.AGENT,
            last_activity_at=datetime.now(UTC),
        ),
    )

    # One open lead per contact, as the index says - and the turn that lost the
    # race still answered its customer, which is the finding.
    assert len(await ai_turns.leads(workspace.tenant_id)) == 1
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied"]
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (RECORD_LEAD_TOOL, ToolExecutionState.SUCCEEDED.value, None)
    ]
    # And the winner's row carries what this turn learned.
    (lead,) = await ai_turns.leads(workspace.tenant_id)
    assert (lead.name, lead.interest) == ("Ahmed", "finishing")


async def test_a_follow_up_that_loses_its_race_costs_the_call_and_not_the_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["talk later"])
    ai_providers.agent = scripted(
        tool_call_response(
            SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still interested?"}
        ),
        text_response(ANSWER),
    )

    await _losing_turn(
        ai_turns,
        ai_providers,
        workspace=workspace,
        conversation_id=conversation_id,
        message_id=ids[0],
        statement=insert(FollowUp).values(
            id=uuid.uuid4(),
            tenant_id=workspace.tenant_id,
            conversation_id=conversation_id,
            scheduled_at=datetime.now(UTC) + timedelta(hours=4),
            body="An earlier nudge.",
            status=FollowUpStatus.PENDING,
        ),
    )

    pending = [
        row
        for row in await ai_turns.follow_ups(workspace.tenant_id)
        if row.status is FollowUpStatus.PENDING
    ]
    assert len(pending) == 1, "the one-pending-nudge rule did not hold"
    # The later intention wins, exactly as rescheduling has always behaved.
    assert pending[0].body == "Still interested?"
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied"]
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (SCHEDULE_FOLLOW_UP_TOOL, ToolExecutionState.SUCCEEDED.value, None)
    ]
