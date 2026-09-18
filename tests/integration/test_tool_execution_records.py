"""What one turn may spend on tools, and what it leaves behind (TOOL-08, TOOL-12, TOOL-19).

Three properties, tested together because they are three views of the same
object: the executor used to run whatever a model response contained, and to
leave a record only when a call both succeeded and mutated something.

**Bounds.** One scripted response of eighty calls executed all eighty: forty
embedding requests, forty audit rows, one lead rewritten forty times, and the
next provider request grown from 2.4 kB to 30 kB. The ceiling was the model's
output-token budget and a 1 MiB body limit - accidents, not decisions.

**Records.** A refusal, a rejected argument, a lifecycle denial, a duplicate
suppression and a call whose turn then died were indistinguishable from a call
that was never requested. "Did this run, and did it have an effect" was not a
question the database could answer.

**The final round.** Calls whose result no round could ever read still ran, so a
customer message could be scheduled in a turn the model never got to reason
about.
"""

from __future__ import annotations

import pytest

from app.agents.orchestrator import MAX_TOOL_CALLS_PER_RESPONSE, MAX_TOOL_CALLS_PER_TURN
from app.agents.registry import HANDOFF_TOOL, RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL
from app.db.models.conversation import ConversationMode
from app.db.models.tool_execution import ToolExecutionReason, ToolExecutionState
from tests.integration.ai_harness import (
    FakeProviders,
    JsonObject,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

ANSWER = "All noted."


async def test_a_response_asking_for_far_too_many_tools_runs_only_the_allowance(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Forty calls in, eight run, the rest are refused and recorded (TOOL-08)."""
    asked = 40
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_calls_response(
            tuple((RECORD_LEAD_TOOL, {"name": f"Ahmed {index}"}) for index in range(asked))
        ),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    # Non-vacuity: the limit was genuinely exceeded, by a wide margin.
    assert len(outcomes) == asked > MAX_TOOL_CALLS_PER_RESPONSE

    ran = [row for row in outcomes if row[1] == ToolExecutionState.SUCCEEDED.value]
    bounded = {
        ToolExecutionReason.RESPONSE_CALL_LIMIT.value,
        ToolExecutionReason.TURN_CALL_LIMIT.value,
    }
    refused = [row for row in outcomes if row[2] in bounded]
    assert len(ran) == MAX_TOOL_CALLS_PER_RESPONSE
    assert len(refused) == asked - MAX_TOOL_CALLS_PER_RESPONSE
    # Both caps are reached, and the response cap is reached first: the calls
    # between the two are refused for the response, the rest for the turn.
    assert [row[2] for row in outcomes].count(
        ToolExecutionReason.RESPONSE_CALL_LIMIT.value
    ) == MAX_TOOL_CALLS_PER_TURN - MAX_TOOL_CALLS_PER_RESPONSE
    # One lead, however many calls were allowed to touch it.
    assert len(await ai_turns.leads(workspace.tenant_id)) == 1
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]


async def test_the_turn_budget_outlives_one_response(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A model fanning out modestly, three rounds running, still stops (TOOL-08)."""
    per_round = MAX_TOOL_CALLS_PER_RESPONSE
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])

    def burst(round_number: int) -> JsonObject:
        return tool_calls_response(
            tuple(
                (RECORD_LEAD_TOOL, {"name": f"Ahmed {round_number}-{index}"})
                for index in range(per_round)
            )
        )

    ai_providers.agent = scripted(burst(1), burst(2), burst(3))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    assert len(outcomes) == per_round * 3, "the rounds did not all ask"
    ran = [row for row in outcomes if row[1] == ToolExecutionState.SUCCEEDED.value]
    assert len(ran) == MAX_TOOL_CALLS_PER_TURN
    assert [row[2] for row in outcomes].count(ToolExecutionReason.TURN_CALL_LIMIT.value) == (
        per_round * 3 - MAX_TOOL_CALLS_PER_TURN
    )


async def test_one_provider_call_id_is_one_execution(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Two calls sharing an id both executed, and both wrote (PD-TOOLS-04)."""
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["talk later"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still there?"}),
                (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 120, "message": "Still there?"}),
            ),
            call_id="call_repeated",
        ),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    # Non-vacuity: the id really did arrive twice.
    assert [row.provider_call_id for row in await ai_turns.executions(workspace.tenant_id)] == [
        "call_repeated",
        "call_repeated",
    ]
    assert outcomes == [
        (SCHEDULE_FOLLOW_UP_TOOL, ToolExecutionState.SUCCEEDED.value, None),
        (
            SCHEDULE_FOLLOW_UP_TOOL,
            ToolExecutionState.DUPLICATE.value,
            ToolExecutionReason.DUPLICATE_CALL.value,
        ),
    ]
    follow_ups = await ai_turns.follow_ups(workspace.tenant_id)
    assert len(follow_ups) == 1
    assert await ai_turns.audit_actions(workspace.tenant_id) == ["agent_follow_up_scheduled"]


async def test_the_final_round_runs_no_tool_whose_answer_nothing_can_read(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A nudge planned on the last round is planned on unverified grounds (TOOL-19)."""
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL, RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_call_response(RECORD_LEAD_TOOL, {"name": "Ahmed"}, text="One moment."),
        tool_call_response(RECORD_LEAD_TOOL, {"interest": "finishing"}),
        tool_calls_response(WRITES_ON_THE_LAST_ROUND),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: three rounds were genuinely used.
    assert ai_providers.inference == 3
    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    assert outcomes[-2:] == [
        (
            RECORD_LEAD_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.ROUND_LIMIT.value,
        ),
        (
            SCHEDULE_FOLLOW_UP_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.ROUND_LIMIT.value,
        ),
    ]
    assert await ai_turns.follow_ups(workspace.tenant_id) == []


WRITES_ON_THE_LAST_ROUND: tuple[tuple[str, JsonObject], ...] = (
    (RECORD_LEAD_TOOL, {"name": "Ahmed Again"}),
    (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still there?"}),
)


async def test_a_handoff_still_runs_on_the_final_round(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Handing over *is* the ending, so it is the one thing still worth doing."""
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL, RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_call_response(RECORD_LEAD_TOOL, {"name": "Ahmed"}),
        tool_call_response(RECORD_LEAD_TOOL, {"interest": "finishing"}),
        tool_call_response(HANDOFF_TOOL, {"reason": "I cannot answer this."}),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 3
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == "I cannot answer this."


async def test_every_call_leaves_a_record_tied_to_the_turn_that_asked(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The join the audit trail could not make (TOOL-12).

    One turn, four calls covering four terminal states: one that ran, one
    refused for want of a grant, one refused for a bad argument, and one
    suppressed as a duplicate. Each is recoverable afterwards, and each names
    the turn, the trigger message, the agent and the conversation.
    """
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (RECORD_LEAD_TOOL, {"name": "Ahmed"}),
                (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "later"}),
                (RECORD_LEAD_TOOL, {"budget_amount": "not a number"}),
            )
        ),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    rows = await ai_turns.executions(workspace.tenant_id)
    assert [(row.tool_name, str(row.state), str(row.reason_code or "")) for row in rows] == [
        (RECORD_LEAD_TOOL, ToolExecutionState.SUCCEEDED.value, ""),
        (
            SCHEDULE_FOLLOW_UP_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.NOT_GRANTED.value,
        ),
        (
            RECORD_LEAD_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.INVALID_ARGUMENTS.value,
        ),
    ]

    for row in rows:
        assert row.tenant_id == workspace.tenant_id
        assert row.conversation_id == conversation_id
        assert row.agent_turn_id is not None
        assert row.trigger_message_id == ids[0]
        assert row.agent_id == workspace.agent_id
        assert row.round_number == 1
        assert row.requested_at is not None
        assert row.finished_at is not None, "a terminal execution must say when it finished"

    # Shapes, never values: the customer's name is on the lead and nowhere here.
    assert [row.argument_fields for row in rows] == [
        ["name"],
        ["delay_minutes", "message"],
        ["budget_amount"],
    ]
    assert rows[0].authorized_at is not None and rows[0].started_at is not None
    assert rows[1].authorized_at is None, "a denied call was never authorised"
