"""Small tool rules that each cost a customer or a colleague something.

**A trigger-less job re-ran everything** (TOOL-17). A turn's identity is the
inbound message it answers, and a job that names none has no way to tell a
redelivery from a second customer message: the same job delivered twice ran two
inferences, scheduled the same nudge twice, wrote two audit rows and sent the
customer two replies. Every enqueue site in this build sets a trigger id, so the
only jobs this can affect are ones an older build left in the queue across a
deploy - and the honest thing to do with work whose effects cannot be bounded is
to stop and say so.

**A chatty model buried its own trail** (TOOL-18). Forty identical
`record_lead_details` calls produced forty `agent_lead_recorded` rows for one
lead. The trail's value is read after an injection report, which is exactly when
signal-to-noise matters most.
"""

from __future__ import annotations

import pytest

from app.agents.registry import RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL
from app.db.models.tool_execution import ToolExecutionState
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

ANSWER = "Noted."


async def test_a_job_with_no_trigger_message_runs_nothing_at_all(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """No identity, no inference, no tool, no reply (TOOL-17, PD-TOOLS-09)."""
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, _ = await ai_turns.write(workspace, ["talk tomorrow"])
    ai_providers.agent = scripted(
        tool_call_response(SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Hi again"}),
        text_response(ANSWER),
    )

    consumed = await ai_turns.answer(workspace, conversation_id, None)

    # Non-vacuity: a job really was delivered and really was picked up.
    assert consumed == 1
    assert ai_providers.inference == 0
    assert ai_providers.sends == []
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert await ai_turns.audit_actions(workspace.tenant_id) == []
    assert await ai_turns.executions(workspace.tenant_id) == []


async def test_a_trigger_less_job_redelivered_still_does_nothing_twice(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The duplicate this closes: two replies to one question."""
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, _ = await ai_turns.write(workspace, ["talk tomorrow"])
    ai_providers.agent = scripted(text_response(ANSWER))

    await ai_turns.enqueue(workspace, conversation_id, None)
    await ai_turns.enqueue(workspace, conversation_id, None)
    consumed = await ai_turns.drain()

    assert consumed == 2, "both jobs were delivered"
    assert ai_providers.sends == []
    assert await ai_turns.outbound(workspace.tenant_id) == []


async def test_a_keyed_job_is_the_control_and_still_answers(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The refusal above is satisfiable by answering nobody. This is not."""
    workspace = await ai_turns.workspace(grants=[SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["talk tomorrow"])
    ai_providers.agent = scripted(text_response(ANSWER))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]


async def test_a_lead_call_that_changed_nothing_writes_no_audit_row(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """One row for the capture that changed something, none for the repeats (TOOL-18)."""
    repeats = 5
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_calls_response(tuple((RECORD_LEAD_TOOL, {"name": "Ahmed"}) for _ in range(repeats))),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: every one of the repeats genuinely ran.
    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    assert len(outcomes) == repeats
    assert {row[1] for row in outcomes} == {ToolExecutionState.SUCCEEDED.value}

    assert await ai_turns.audit_actions(workspace.tenant_id) == ["agent_lead_recorded"]
    assert len(await ai_turns.leads(workspace.tenant_id)) == 1


async def test_a_lead_call_that_did_change_something_is_still_recorded(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The control. Suppressing the no-ops must not suppress the real ones."""
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (RECORD_LEAD_TOOL, {"name": "Ahmed"}),
                (RECORD_LEAD_TOOL, {"interest": "finishing a flat"}),
                (RECORD_LEAD_TOOL, {"budget_amount": 500000, "budget_currency": "EGP"}),
            )
        ),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert await ai_turns.audit_actions(workspace.tenant_id) == [
        "agent_lead_recorded",
        "agent_lead_recorded",
        "agent_lead_recorded",
    ]
    lead = (await ai_turns.leads(workspace.tenant_id))[0]
    assert (lead.name, lead.interest, str(lead.budget_currency)) == (
        "Ahmed",
        "finishing a flat",
        "EGP",
    )
