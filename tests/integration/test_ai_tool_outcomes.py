"""A handoff is something the server did, not a word the model said (AI-03).

The defect was one line: `if call.name == HANDOFF_TOOL: handed_off = True`. The
grant check correctly refused an agent that had never been given the tool - and
the turn was silenced anyway. No reply, no outbound row, the conversation still
in `AI` mode with no reason recorded, the turn `COMPLETED`: a customer answered
by silence and invisible in every inbox.

These run the real worker against the real registry, so "the handoff happened"
is asserted on the conversation row and "the reply went out" on the outbound
row, rather than on a flag the orchestrator returns.
"""

from __future__ import annotations

import json

import pytest

from app.agents.registry import HANDOFF_TOOL
from app.db.models.conversation import ConversationMode
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration


async def test_a_model_naming_an_ungranted_handoff_is_still_answered(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()  # granted nothing, like a new agent
    conversation_id, ids = await ai_turns.write(workspace, ["get me a human"])
    ai_providers.agent = scripted(
        tool_call_response(HANDOFF_TOOL, {"reason": "asked"}, text="One moment."),
        text_response("I can help you with that here."),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the refusal genuinely reached the model on a second round.
    assert ai_providers.inference == 2
    assert "not available to this agent" in json.dumps(ai_providers.agent_requests[1]["input"])

    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [
        "I can help you with that here."
    ]
    assert len(ai_providers.sends) == 1
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.AI
    assert conversation.handoff_reason is None
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]


async def test_a_granted_handoff_hands_over_and_sends_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The control case: the same model output, with the grant, really does hand over."""
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["get me a human"])
    ai_providers.agent = scripted(
        tool_call_response(
            HANDOFF_TOOL, {"reason": "The customer asked for a person."}, text="One moment."
        ),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.outbound(workspace.tenant_id) == []
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == "The customer asked for a person."


async def test_a_handoff_call_with_invalid_arguments_is_answered_normally(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["hmm"])
    ai_providers.agent = scripted(
        tool_call_response(HANDOFF_TOOL, {}, text="One moment."),
        text_response("Could you tell me a little more?"),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 2
    assert len(ai_providers.sends) == 1
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.AI
