"""What a model can put in a tool argument, and what that must never cost (TOOL-01).

Three classes of value a model can legitimately emit were accepted by the
registry, refused by PostgreSQL or by Python, and contained by nothing: a NUL
character, a lone surrogate, and a `delay_minutes` large enough to overflow a
`timedelta`. Each one ended the customer's turn - no reply, no handoff, no
explanation, the conversation still in AI mode so nobody was asked to pick it
up, the turn charged, and any earlier round's writes still committed. All three
are reachable from an ordinary model mistake and from ordinary prompt injection
("reply using a null character", "follow up in 10^15 minutes").

Every test here drives the real `AgentWorker` against the real registry, so the
claim being made is about the customer's turn rather than about a function's
return value: **a reply was sent, and the turn completed**.

The Arabic control is not decoration. The rule is about text the storage layer
cannot hold, not about alphabets, and a fix that refused Arabic, RTL marks or
emoji would be a worse bug than the one it closed.
"""

from __future__ import annotations

import json

import pytest

from app.agents.registry import (
    HANDOFF_TOOL,
    RECORD_LEAD_TOOL,
    SCHEDULE_FOLLOW_UP_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
)
from app.db.models.agent_turn import TurnOutcome
from app.db.models.conversation import ConversationMode
from app.db.models.tool_execution import ToolExecutionReason, ToolExecutionState
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration

ANSWER = "Noted, thank you."

# Values PostgreSQL cannot store, values Python cannot convert, and a string
# longer than the column it would land in. One per class, each on a different
# tool, so the coverage is the argument boundary rather than one handler.
UNUSABLE: list[tuple[str, str, dict[str, object], ToolExecutionReason]] = [
    (
        "nul in a handoff reason",
        HANDOFF_TOOL,
        {"reason": "bad\x00reason"},
        ToolExecutionReason.UNSAFE_TEXT,
    ),
    (
        "nul in a lead name",
        RECORD_LEAD_TOOL,
        {"name": "Ah\x00med"},
        ToolExecutionReason.UNSAFE_TEXT,
    ),
    (
        "lone surrogate in a lead interest",
        RECORD_LEAD_TOOL,
        {"interest": "flat \ud800"},
        ToolExecutionReason.UNSAFE_TEXT,
    ),
    (
        "nul in a follow-up body",
        SCHEDULE_FOLLOW_UP_TOOL,
        {"delay_minutes": 60, "message": "hi\x00"},
        ToolExecutionReason.UNSAFE_TEXT,
    ),
    (
        "a delay no timedelta can hold",
        SCHEDULE_FOLLOW_UP_TOOL,
        {"delay_minutes": 1_000_000_000_000_000, "message": "later"},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
    (
        "a negative delay",
        SCHEDULE_FOLLOW_UP_TOOL,
        {"delay_minutes": -5, "message": "later"},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
    (
        "a lead name longer than its column",
        RECORD_LEAD_TOOL,
        {"name": "n" * 5_000},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
    (
        "a follow-up body longer than WhatsApp accepts",
        SCHEDULE_FOLLOW_UP_TOOL,
        {"delay_minutes": 60, "message": "m" * 50_000},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
    (
        "a currency code that is not one",
        RECORD_LEAD_TOOL,
        {"budget_amount": 500, "budget_currency": "egyptian pounds"},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
    (
        "more passages than the server will ever return",
        SEARCH_KNOWLEDGE_TOOL,
        {"query": "prices", "max_results": 4_000},
        ToolExecutionReason.RANGE_VIOLATION,
    ),
]


@pytest.mark.parametrize(
    ("case", "tool", "arguments", "reason"),
    UNUSABLE,
    ids=[row[0] for row in UNUSABLE],
)
async def test_an_argument_the_server_cannot_use_costs_the_call_and_not_the_turn(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    case: str,
    tool: str,
    arguments: dict[str, object],
    reason: ToolExecutionReason,
) -> None:
    """Each of these used to be a customer answered by silence."""
    workspace = await ai_turns.workspace(grants=[tool])
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(
        tool_call_response(tool, arguments, text="One moment."),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the offending argument genuinely reached the provider's wire
    # and the refusal genuinely came back on a second round.
    assert ai_providers.inference == 2, "the model never got a second round"
    refusal = json.dumps(ai_providers.agent_requests[1]["input"])
    assert "Argument" in refusal

    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [TurnOutcome.REPLIED.value]
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]

    # And the refusal is on the record, under the reason an operator counts.
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (tool, ToolExecutionState.REJECTED.value, reason.value)
    ]

    # Nothing was written by a call that never ran.
    assert await ai_turns.leads(workspace.tenant_id) == []
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.AI
    assert await ai_turns.audit_actions(workspace.tenant_id) == []


async def test_arabic_rtl_and_emoji_are_stored_exactly_as_the_model_wrote_them(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The control. The rule is about storability, never about alphabet."""
    name = "‏أحمد 😀"
    interest = "تشطيب شقة 150م"
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["أنا أحمد"])
    ai_providers.agent = scripted(
        tool_call_response(RECORD_LEAD_TOOL, {"name": name, "interest": interest}),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    leads = await ai_turns.leads(workspace.tenant_id)
    assert [(lead.name, lead.interest) for lead in leads] == [(name, interest)]
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (RECORD_LEAD_TOOL, ToolExecutionState.SUCCEEDED.value, None)
    ]


async def test_a_handoff_reason_at_the_published_limit_still_hands_over(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The bound is inclusive, and the tool at it behaves normally (TM09)."""
    reason = "r" * 200
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["get me a person"])
    ai_providers.agent = scripted(tool_call_response(HANDOFF_TOOL, {"reason": reason}))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == reason


async def test_one_character_past_the_limit_is_refused_rather_than_truncated(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A silent truncation loses the end of a sentence a colleague will read.

    It used to: the reason was cut to two hundred characters and nothing told
    the model, so the colleague taking the conversation over read half an
    explanation (TM09).
    """
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["get me a person"])
    ai_providers.agent = scripted(
        tool_call_response(HANDOFF_TOOL, {"reason": "r" * 201}),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.AI
    assert conversation.handoff_reason is None
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
