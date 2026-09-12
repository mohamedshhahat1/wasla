"""An agent stops acting the moment its workspace, agent or conversation says so.

AI-06: nothing on the AI path read a workspace's lifecycle. A suspended workspace
- the operator's tool for abuse and payment disputes - and a deleted one, for the
whole of its 30-day retention, kept running inference and messaging customers.

AI-07: after a long inference only the conversation's mode was read again. An
agent disabled, a conversation closed or a workspace suspended while the model was
composing still got its reply sent.

Every "during inference" change here is made from *inside* the fake provider's
handler, on a connection of its own and committed, so it lands strictly between
the turn's last read and its send. Every suppression is paired with proof the
turn really reached the provider, and every ending is asserted as a recorded
outcome rather than inferred from an absence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import update

from app.db.models.agent import Agent, AgentStatus
from app.db.models.billing import LimitKey
from app.db.models.conversation import Conversation, ConversationMode, ConversationStatus
from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from tests.integration.ai_harness import (
    AgentHandler,
    FakeProviders,
    JsonObject,
    TurnRunner,
    Workspace,
    scripted,
    text_response,
)

pytestmark = pytest.mark.integration

REPLY = "Here is the answer you asked for."


def _changing_during_inference(runner: TurnRunner, statement: Any) -> AgentHandler:
    """A provider that commits `statement` elsewhere while it is composing."""

    async def handle(_request: JsonObject) -> JsonObject:
        await runner.execute(statement)
        return text_response(REPLY, response_id="resp_changed_mid_turn")

    return handle


async def _one_turn(runner: TurnRunner, workspace: Workspace) -> Any:
    conversation_id, ids = await runner.write(workspace, ["hello?"])
    assert await runner.answer(workspace, conversation_id, ids[0]) == 1
    return conversation_id


# ------------------------------------------------------ before the turn starts


async def test_a_suspended_workspace_makes_no_provider_call_and_sends_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    await ai_turns.execute(
        update(Tenant).where(Tenant.id == workspace.tenant_id).values(status=TenantStatus.SUSPENDED)
    )

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1

    assert (ai_providers.sentiment, ai_providers.inference, len(ai_providers.sends)) == (0, 0, 0)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_workspace"]
    assert "ai_turn" not in await ai_turns.usage(workspace.tenant_id)


async def test_a_deleted_workspace_makes_no_provider_call_and_sends_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Retention controls when data is erased; it does not keep the AI serving."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    await ai_turns.execute(
        update(Tenant).where(Tenant.id == workspace.tenant_id).values(deleted_at=datetime.now(UTC))
    )

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1

    assert (ai_providers.sentiment, ai_providers.inference, len(ai_providers.sends)) == (0, 0, 0)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_workspace"]


async def test_a_conversation_closed_before_the_turn_is_not_answered(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    await ai_turns.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(status=ConversationStatus.CLOSED)
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert (ai_providers.inference, len(ai_providers.sends)) == (0, 0)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_closed"]
    assert (await ai_turns.conversation(conversation_id)).status is ConversationStatus.CLOSED


# ------------------------------------------------------ while the model composes


async def test_a_workspace_suspended_during_inference_gets_no_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(Tenant)
        .where(Tenant.id == workspace.tenant_id)
        .values(status=TenantStatus.SUSPENDED),
    )

    await _one_turn(ai_turns, workspace)

    assert ai_providers.inference == 1, "the turn really reached the provider"
    assert ai_providers.sends == []
    assert await ai_turns.outbound(workspace.tenant_id) == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_workspace"]


async def test_a_workspace_deleted_during_inference_gets_no_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(Tenant).where(Tenant.id == workspace.tenant_id).values(deleted_at=datetime.now(UTC)),
    )

    await _one_turn(ai_turns, workspace)

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_workspace"]


async def test_an_agent_disabled_during_inference_sends_no_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(Agent).where(Agent.id == workspace.agent_id).values(status=AgentStatus.DISABLED),
    )

    await _one_turn(ai_turns, workspace)

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_agent"]


async def test_a_conversation_closed_during_inference_is_not_reopened_by_a_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(status=ConversationStatus.CLOSED),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.outbound(workspace.tenant_id) == []
    assert (await ai_turns.conversation(conversation_id)).status is ConversationStatus.CLOSED
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_closed"]


async def test_a_conversation_taken_over_during_inference_sends_no_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(mode=ConversationMode.HUMAN),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_human"]


async def test_a_number_disabled_during_inference_is_suppressed_not_stranded(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The audit measured this one ending `ENGAGED` and dead-lettered."""
    workspace = await ai_turns.workspace()
    ai_providers.agent = _changing_during_inference(
        ai_turns,
        update(WhatsAppAccount)
        .where(WhatsAppAccount.id == workspace.account_id)
        .values(status=WhatsAppAccountStatus.DISABLED),
    )

    await _one_turn(ai_turns, workspace)

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_channel"]


# ------------------------------------------------------------ recorded endings


async def test_an_ordinary_reply_is_recorded_with_the_providers_response_id(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.agent = scripted(text_response(REPLY, response_id="resp_known_0001"))

    await _one_turn(ai_turns, workspace)

    assert len(ai_providers.sends) == 1
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied"]
    assert await ai_turns.response_ids(workspace.tenant_id) == ["resp_known_0001"]


async def test_an_escalation_is_recorded_as_one(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.reading = {
        "sentiment": "angry",
        "score": -0.95,
        "intent": "complaint",
        "confidence": 0.95,
    }

    conversation_id = await _one_turn(ai_turns, workspace)

    assert ai_providers.sentiment == 1
    assert ai_providers.inference == 0
    assert ai_providers.sends == []
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.HUMAN
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["escalated"]


async def test_a_refused_allowance_is_recorded_as_quota_blocked(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({LimitKey.PERIOD_AI_TURNS.value: 0})
    workspace = await ai_turns.workspace()

    await _one_turn(ai_turns, workspace)

    assert ai_providers.inference == 0
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["quota_blocked"]


async def test_a_workspace_with_no_answering_agent_records_why(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(status=AgentStatus.DRAFT)

    await _one_turn(ai_turns, workspace)

    assert (ai_providers.sentiment, ai_providers.inference) == (0, 0)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["suppressed_agent"]
