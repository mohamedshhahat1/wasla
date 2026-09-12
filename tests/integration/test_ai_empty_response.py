"""A model that answers with nothing does not leave a customer answered by silence.

PD-3, measured before this change: the provider returned a well-formed response
with no text, the orchestrator reported nothing worth saying, the turn completed,
and the customer received nothing while no alert, dead letter or inbox entry
said anything had gone wrong. A provider that started returning refusal parts
instead of `output_text` would have silenced every customer on the platform with
every dashboard green.

Now the ending is explicit on both sides: the customer is told, in their own
language, that a colleague will follow up; a colleague is handed the
conversation with a reason saying why; and the turn records `empty_response`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.agents.registry import SEARCH_KNOWLEDGE_TOOL
from app.agents.reply import ARABIC_FALLBACK, ENGLISH_FALLBACK
from app.db.models.conversation import ConversationMode
from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.workers.ai_worker import EMPTY_RESPONSE_HANDOFF_REASON
from tests.integration.ai_harness import (
    FakeProviders,
    JsonObject,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration


async def test_an_empty_answer_tells_the_customer_and_hands_the_conversation_over(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["Do you finish apartments?"])
    ai_providers.agent = scripted(text_response(None))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1, "the provider genuinely answered, with nothing"
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ENGLISH_FALLBACK]
    assert len(ai_providers.sends) == 1
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == EMPTY_RESPONSE_HANDOFF_REASON
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["empty_response"]


async def test_an_arabic_speaking_customer_is_told_in_arabic(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["عايز اعرف سعر التشطيب"])
    ai_providers.agent = scripted(text_response(None))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ARABIC_FALLBACK]


async def test_a_refusal_part_instead_of_text_is_an_empty_answer_too(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The provider-shape change the audit named: every customer silenced at once."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    refusal: JsonObject = text_response(None)
    refusal["output"] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "I can't help with that."}],
        }
    ]
    ai_providers.agent = scripted(refusal)

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert len(ai_providers.sends) == 1
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["empty_response"]


async def test_a_model_that_only_ever_asks_for_tools_is_not_silent(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Three rounds of tool calls and not one word: the round limit, then nothing."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(tool_call_response(SEARCH_KNOWLEDGE_TOOL, {"query": "x"}))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 3, "the round limit was genuinely reached"
    assert len(ai_providers.sends) == 1
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["empty_response"]


async def test_an_empty_answer_for_a_workspace_suspended_mid_turn_sends_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The fallback is a customer message like any other, and is revalidated like one."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    async def suspend_then_say_nothing(_request: JsonObject) -> JsonObject:
        await ai_turns.execute(
            update(Tenant)
            .where(Tenant.id == workspace.tenant_id)
            .values(status=TenantStatus.SUSPENDED)
        )
        return text_response(None)

    ai_providers.agent = suspend_then_say_nothing

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.AI
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_workspace"]
