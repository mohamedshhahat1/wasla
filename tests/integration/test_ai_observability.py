"""A degraded AI provider, and an agent turn that went wrong, are visible (AI-09).

What the audit found: provider metrics recorded one final outcome per call, so
throttling the retry absorbed looked like success; the classifier and the agent
shared one operation label; nothing counted turns stranded `ENGAGED`; and the
silent endings emitted a warning line and no series. A hard outage was visible
through dead letters and a degraded one was not.

Real Redis as the counter sink and real PostgreSQL behind the exposition, so
what is asserted is what a scrape would read.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.metrics import MetricsRegistry
from app.core.telemetry import read_redis_counters, set_counter_sink
from app.db.models.agent_turn import AgentTurn, AgentTurnState
from app.integrations.openai.client import RESPOND_AGENT, ResponsesClient
from app.integrations.openai.types import Turn
from app.services.metrics_service import MetricsService
from tests.integration.ai_harness import REDIS_URL, FakeProviders, TurnRunner, text_response

pytestmark = pytest.mark.integration


async def _clear(client: Redis) -> None:
    async for key in client.scan_iter(match="metrics:*"):
        await client.delete(key)


@pytest_asyncio.fixture
async def counters() -> AsyncIterator[Redis]:
    """The process-wide counter sink, pointed at a Redis these tests own."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await _clear(client)
    set_counter_sink(client)
    try:
        yield client
    finally:
        set_counter_sink(None)
        await _clear(client)
        await client.aclose()


async def _by(client: Redis, metric: str, label: str, **matching: str) -> dict[str, float]:
    collected = await read_redis_counters(client)
    return {
        labels[label]: value
        for labels, value in collected.get(metric, [])
        if all(labels.get(key) == wanted for key, wanted in matching.items())
    }


async def _no_wait(_seconds: float) -> None:
    return None


async def test_a_call_throttled_twice_is_not_counted_as_a_clean_success(
    counters: Redis,
) -> None:
    answers = [
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}),
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}),
        httpx.Response(200, json=text_response("ok")),
    ]
    transport = httpx.MockTransport(lambda _request: answers.pop(0))

    async with httpx.AsyncClient(transport=transport) as http:
        client = ResponsesClient(http=http, api_key="sk-test-not-real", sleep=_no_wait)
        reply = await client.respond(
            model="gpt-4o-mini", instructions="", turns=[Turn(role="user", text="hi")]
        )

    assert reply.text == "ok"
    assert answers == [], "all three answers were genuinely consumed"
    attempts = await _by(
        counters, "wasla_provider_attempts_total", "outcome", operation=RESPOND_AGENT
    )
    assert attempts == {"rate_limited": 2.0, "success": 1.0}
    calls = await _by(counters, "wasla_provider_requests_total", "outcome", operation=RESPOND_AGENT)
    assert calls == {"success": 1.0}


async def test_the_classifier_and_the_agent_are_counted_apart(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert (ai_providers.sentiment, ai_providers.inference) == (1, 1)
    operations = await _by(
        counters, "wasla_provider_requests_total", "operation", provider="openai"
    )
    assert operations == {"respond_sentiment": 1.0, "respond_agent": 1.0}


async def test_every_turn_ending_is_counted_by_outcome(
    ai_turns: TurnRunner, ai_providers: FakeProviders, counters: Redis
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert len(ai_providers.sends) == 1
    outcomes = await _by(counters, "wasla_agent_turn_outcomes_total", "outcome")
    assert outcomes == {"replied": 1.0}


def _gauge(rendered: str, name: str) -> float:
    match = re.search(rf"^{name} (\S+)$", rendered, flags=re.MULTILINE)
    assert match is not None, f"{name} is missing from the exposition"
    return float(match.group(1))


async def test_a_turn_stranded_engaged_is_counted_once_it_is_old_enough(
    ai_turns: TurnRunner,
) -> None:
    """An old engaged turn counts; a turn in flight right now does not."""
    workspace = await ai_turns.workspace()
    conversation_id, _ = await ai_turns.write(workspace, ["hello"])
    service = MetricsService(None, registry=MetricsRegistry(), database=ai_turns.database)
    now = datetime.now(UTC)
    before = _gauge(await service.render(now=now), "wasla_agent_turns_engaged_unfinished")

    async with ai_turns.database.session() as session:
        for engaged_at, state in (
            (now - timedelta(hours=1), AgentTurnState.ENGAGED),
            (now - timedelta(minutes=1), AgentTurnState.ENGAGED),
            (now - timedelta(hours=2), AgentTurnState.COMPLETED),
        ):
            session.add(
                AgentTurn(
                    tenant_id=workspace.tenant_id,
                    conversation_id=conversation_id,
                    trigger_message_id=uuid.uuid4(),
                    state=state,
                    engaged_at=engaged_at,
                )
            )

    rendered = await service.render(now=now)

    assert _gauge(rendered, "wasla_agent_turns_engaged_unfinished") == before + 1
    assert _gauge(rendered, "wasla_oldest_engaged_agent_turn_age_seconds") >= 3_500
