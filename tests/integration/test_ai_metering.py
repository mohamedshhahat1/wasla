"""One customer turn spends one unit of the plan; every provider call is still paid for.

AI-02 in one sentence: the plan's AI allowance was counted in provider requests,
the sentiment classifier made one of those, and nothing checked it. With exactly
one unit left the classifier took it, the inference was refused, the turn was
marked `COMPLETED`, and the customer was never answered - four concurrent turns
against an allowance of one produced four classifications and zero replies.

So two ledgers are pinned here, separately, because they answer different
questions. `ai_turn` is what the customer bought and is enforced; `ai_request`
and the token meters are what the platform paid for and are recorded exactly.
Every test runs the real worker against real PostgreSQL and Redis, with only the
provider hosts faked at the transport.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.agents.registry import SEARCH_KNOWLEDGE_TOOL
from app.db.models.billing import LimitKey
from app.db.models.conversation import Conversation, ConversationMode
from app.db.models.usage import UsageEvent, UsageEventType
from app.workers.ai_worker import QUOTA_HANDOFF_REASON
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration

TURNS = LimitKey.PERIOD_AI_TURNS.value


async def test_no_allowance_means_no_provider_call_and_a_person_is_told(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Refused before anything is spent, and never silently."""
    await ai_turns.plan({TURNS: 0})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1

    assert (ai_providers.sentiment, ai_providers.inference, len(ai_providers.sends)) == (0, 0, 0)
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == QUOTA_HANDOFF_REASON
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    usage = await ai_turns.usage(workspace.tenant_id)
    assert "ai_turn" not in usage
    assert "ai_request" not in usage


async def test_an_allowance_of_one_turn_answers_one_customer(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The boundary the audit measured: one unit left used to buy a classification."""
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sentiment == 1
    assert ai_providers.inference == 1
    assert len(ai_providers.sends) == 1
    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_turn"] == 1
    assert usage["ai_request"] == 2, "the classifier and the round are still paid for"
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.AI


async def test_concurrent_turns_never_oversell_the_turn_allowance(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Five customers, five workers on five connection pools, two turns to sell.

    Exactly two are answered and three reach a person. The blocked three make no
    provider call at all - not a classification, not an inference - which is the
    difference from the audit's measurement, where all four blocked turns were
    classified and billed.
    """
    allowance, customers = 2, 5
    await ai_turns.plan({TURNS: allowance})
    workspace = await ai_turns.workspace()
    conversations = []
    for index in range(customers):
        conversation_id, ids = await ai_turns.write(
            workspace, ["hello"], wa_id=f"20155510{index:04d}"
        )
        await ai_turns.enqueue(workspace, conversation_id, ids[0])
        conversations.append(conversation_id)

    assert await ai_turns.race(customers) == customers, "every envelope must be taken"

    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_turn"] == allowance
    assert ai_providers.inference == allowance
    assert ai_providers.sentiment == allowance
    assert len(ai_providers.sends) == allowance
    modes = [(await ai_turns.conversation(c)).mode for c in conversations]
    assert modes.count(ConversationMode.HUMAN) == customers - allowance
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"] * customers


async def test_a_tool_using_turn_is_still_one_customer_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["what are your prices?"])
    ai_providers.agent = scripted(
        tool_call_response(SEARCH_KNOWLEDGE_TOOL, {"query": "prices"}),
        text_response("Here you go."),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 2, "the tool round genuinely happened"
    assert len(ai_providers.sends) == 1
    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_turn"] == 1
    assert usage["ai_request"] == 3


async def test_duplicate_jobs_consume_one_customer_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.enqueue(workspace, conversation_id, ids[0])
    await ai_turns.enqueue(workspace, conversation_id, ids[0])

    assert await ai_turns.drain() == 2, "both envelopes must reach the worker"

    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_turn"] == 1
    assert usage["ai_request"] == 2
    assert (ai_providers.sentiment, ai_providers.inference, len(ai_providers.sends)) == (1, 1, 1)


async def test_one_plain_turn_meters_its_provider_calls_and_tokens_exactly(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Pins the cost ledger, so a request recorded twice cannot ship silently."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert (ai_providers.sentiment, ai_providers.inference) == (1, 1)
    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_request"] == 2
    # The fake's classification reports 3 in / 2 out; its agent round 11 / 5.
    assert usage["ai_input_token"] == 14
    assert usage["ai_output_token"] == 7


async def test_each_provider_request_says_what_it_was_for(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    async with ai_turns.database.session() as session:
        rows = await session.scalars(
            select(UsageEvent.meta).where(
                UsageEvent.tenant_id == workspace.tenant_id,
                UsageEvent.event_type == UsageEventType.AI_REQUEST,
            )
        )
        purposes = sorted(str((meta or {}).get("purpose")) for meta in rows)
    # One row per provider call. The worker's end-of-turn record carries the
    # tokens with `requests=0`, which writes no request row at all.
    assert purposes == ["agent", "sentiment"]


async def test_a_conversation_a_person_owns_costs_no_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    async with ai_turns.database.session() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(mode=ConversationMode.HUMAN)
        )

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1

    assert (ai_providers.sentiment, ai_providers.inference) == (0, 0)
    assert "ai_turn" not in await ai_turns.usage(workspace.tenant_id)
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
