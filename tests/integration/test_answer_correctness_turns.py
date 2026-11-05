"""Number grounding, pre-retrieval and parallel sentiment through the real worker (FX-3.13 .. 3.15).

The worker resolves the workspace's knowledge switches from its settings, runs
the orchestrator and sends through the channel; the model, the classifier, the
embeddings and Meta are scripted at their HTTP transports. What a test asserts
was sent is what reached the fake channel transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.telemetry import read_redis_counters, set_counter_sink
from app.db.models.conversation import ConversationMode
from app.services.knowledge_service import KnowledgeService
from tests.fake_embeddings import FAKE_MODEL, FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)
from tests.integration.rag_harness import REDIS_URL

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def counters() -> AsyncIterator[Redis]:
    """The process-wide counter sink, pointed at a Redis these tests own."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    async for key in client.scan_iter(match="metrics:*"):
        await client.delete(key)
    set_counter_sink(client)
    try:
        yield client
    finally:
        set_counter_sink(None)
        async for key in client.scan_iter(match="metrics:*"):
            await client.delete(key)
        await client.aclose()


SEARCH = "search_knowledge"
LEAD = "record_lead_details"
PRICES = "Premium finishing costs 7900 EGP per square metre including materials."
QUESTION = "how much is premium finishing per square metre please"


async def _index(runner: TurnRunner, tenant_id: Any, body: str = PRICES) -> None:
    async with runner.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(knowledge_base_id=base.id, title="Prices", raw=body)
        await knowledge.ingest(
            document_id=view.document.id, embeddings=as_embeddings(FakeEmbeddings())
        )


async def _turn(runner: TurnRunner, workspace: Any, text: str = QUESTION) -> Any:
    conversation_id, (message_id,) = await runner.write(workspace, [text])
    await runner.answer(workspace, conversation_id, message_id)
    return conversation_id


def _sent(providers: FakeProviders) -> list[str]:
    return [send.get("text", {}).get("body", "") for send in providers.sends]


async def _grounding(counters: Redis) -> dict[str, float]:
    collected = await read_redis_counters(counters)
    return {
        labels["outcome"]: value
        for labels, value in collected.get("wasla_rag_number_grounding_total", [])
    }


def _search_then(*replies: str) -> Any:
    return scripted(
        tool_call_response(SEARCH, {"query": "premium finishing price per square metre"}),
        *[text_response(reply) for reply in replies],
    )


# ------------------------------------------------------------------ grounding


async def test_observe_counts_an_ungrounded_price_and_sends_the_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = _search_then("Premium finishing is 8,400 EGP per square metre.")

    await _turn(ai_turns, workspace)

    # Observe changes nothing: the number reached the channel transport.
    assert any("8,400 EGP" in body for body in _sent(ai_providers))
    assert (await _grounding(counters)).get("ungrounded") == 1.0


async def test_a_grounded_price_is_counted_grounded(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = _search_then("Premium finishing is 7,900 EGP per square metre.")

    await _turn(ai_turns, workspace)

    assert any("7,900 EGP" in body for body in _sent(ai_providers))
    assert await _grounding(counters) == {"grounded": 1.0}


async def test_correct_mode_sends_the_corrected_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    ai_turns.configure(rag_number_grounding="correct")
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = _search_then(
        "Premium finishing is 8,400 EGP per square metre.",
        "Premium finishing is 7,900 EGP per square metre.",
    )

    await _turn(ai_turns, workspace)

    assert _sent(ai_providers) == ["Premium finishing is 7,900 EGP per square metre."]
    corrective = ai_providers.agent_requests[-1]
    assert corrective["tool_choice"] == "none"
    assert "8400" in json.dumps(corrective["input"])
    assert (await _grounding(counters)).get("corrected") == 1.0


async def test_strict_mode_withholds_the_reply_and_hands_over(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    ai_turns.configure(rag_number_grounding="strict")
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = _search_then("Premium finishing is 8,400 EGP per square metre.")

    conversation_id = await _turn(ai_turns, workspace)

    sent = _sent(ai_providers)
    assert len(sent) == 1 and "8,400" not in sent[0]
    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert (conversation.handoff_reason or "").startswith("AI_UNGROUNDED_NUMBER")
    assert (await _grounding(counters)).get("handed_off") == 1.0


# -------------------------------------------------------------- pre-retrieval


async def test_pre_retrieval_answers_in_one_round_and_is_a_recorded_execution(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-R44: the search before round one is a `tool_executions` row like any other."""
    ai_turns.configure(
        rag_preretrieval_enabled=True,
        rag_confidence_tiers_enabled=True,
        rag_confidence_tiers={FAKE_MODEL: (0.6, 0.9)},
    )
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = scripted(text_response("Premium is 7,900 EGP per square metre."))

    await _turn(ai_turns, workspace)

    assert ai_providers.inference == 1
    (execution,) = await ai_turns.executions(workspace.tenant_id)
    assert (execution.tool_name, execution.round_number, execution.call_ordinal) == (SEARCH, 1, 0)
    assert str(execution.state) == "succeeded"
    items = ai_providers.agent_requests[0]["input"]
    calls = [i for i in items if i.get("type") == "function_call"]
    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert [c["name"] for c in calls] == [SEARCH]
    assert calls[0]["call_id"] == outputs[0]["call_id"] == execution.provider_call_id
    # The passages stay inside the tool output (G5).
    assert "7900 EGP" in outputs[0]["output"]
    assert "7900 EGP" not in json.dumps([i for i in items if i not in outputs])


async def test_pre_retrieval_does_nothing_while_tiers_are_off(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    ai_turns.configure(rag_preretrieval_enabled=True)
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.embedding_requests.clear()
    ai_providers.agent = scripted(text_response("Let me help."))

    await _turn(ai_turns, workspace)

    assert await ai_turns.executions(workspace.tenant_id) == []
    assert ai_providers.embedding_requests == []
    assert not [
        i for i in ai_providers.agent_requests[0]["input"] if i.get("type") == "function_call"
    ]


# ---------------------------------------------------------- parallel sentiment


ANGRY = {"sentiment": "angry", "score": -0.95, "intent": "complaint", "confidence": 0.95}


async def test_an_escalation_arriving_after_round_one_sends_nothing_and_runs_no_tool(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    ai_turns.configure(rag_parallel_sentiment_enabled=True)
    workspace = await ai_turns.workspace(grants=[SEARCH, LEAD])
    ai_providers.reading = ANGRY
    round_one_answered = asyncio.Event()

    async def agent(_body: dict[str, Any]) -> Any:
        round_one_answered.set()
        return tool_call_response(LEAD, {"name": "Ahmed"})

    async def after_round_one() -> None:
        # The classification arrives second: round one has already answered.
        await round_one_answered.wait()
        await asyncio.sleep(0.05)

    ai_providers.agent = agent
    ai_providers.sentiment_gate = after_round_one

    conversation_id = await _turn(ai_turns, workspace, "this is unacceptable, I want my money")

    assert (ai_providers.inference, ai_providers.sentiment) == (1, 1)
    assert ai_providers.sends == []
    assert await ai_turns.executions(workspace.tenant_id) == []
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.HUMAN
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["escalated"]


async def test_a_calm_message_in_parallel_is_answered_and_its_reading_stored(
    ai_turns: TurnRunner, ai_providers: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import func, select

    from app.db.models.sentiment import MessageSentiment as Reading

    ai_turns.configure(rag_parallel_sentiment_enabled=True)
    workspace = await ai_turns.workspace()
    agent_in, classifier_in = asyncio.Event(), asyncio.Event()
    window = {"open": False, "used": 0, "total": 0}

    # No AsyncSession is used while both calls are in flight (RQ-20).
    from sqlalchemy.ext.asyncio import AsyncSession

    for name in ("execute", "scalar", "scalars", "flush", "commit", "get"):
        original = getattr(AsyncSession, name)

        def guarded(*args: Any, __original: Any = original, **kwargs: Any) -> Any:
            window["total"] += 1
            if window["open"]:
                window["used"] += 1
            return __original(*args, **kwargs)

        monkeypatch.setattr(AsyncSession, name, guarded)

    async def agent(_body: dict[str, Any]) -> Any:
        # Each call waits for the other to arrive: only two calls in flight
        # together can both get past this. Serially, the first would time out.
        agent_in.set()
        await asyncio.wait_for(classifier_in.wait(), timeout=5)
        window["open"] = True
        await asyncio.sleep(0.05)
        window["open"] = False
        return text_response("Happy to help.")

    async def overlap() -> None:
        classifier_in.set()
        await asyncio.wait_for(agent_in.wait(), timeout=5)

    ai_providers.agent = agent
    ai_providers.sentiment_gate = overlap

    await _turn(ai_turns, workspace, "hello, a question about delivery please")

    assert agent_in.is_set() and classifier_in.is_set()
    # Non-vacuity: the guard saw the turn's database work, outside the window.
    assert window["total"] > 0
    assert window["used"] == 0
    assert len(ai_providers.sends) == 1
    async with ai_turns.database.session() as session:
        stored = await session.scalar(
            select(func.count(Reading.id)).where(Reading.tenant_id == workspace.tenant_id)
        )
    assert stored == 1
