"""The last round of a turn ends in words, and rounds replay in order (RV-14, RV-27, RQ-10).

P-R11: a model that searched on rounds one and two and asked to search again on
round three was offered every tool on that round, then had the call refused for
lack of a round to read it - so the turn ended empty, or with "let me check"
sent as the whole reply.

P-R12: the request for round three replayed every assistant preamble first and
every tool exchange after them, so the provider read the rounds out of order and
no round's request began with the previous round's.

The order test runs the real `ResponsesClient` over `httpx.MockTransport` and
asserts the bytes handed to the transport, not an internal list.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agents.registry import (
    HANDOFF_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
    ToolDefinition,
    ToolRegistry,
)
from app.db.models.agent_turn import TurnOutcome
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.types import AgentReply, ToolCall
from tests.fakes import as_http_client
from tests.unit.test_agent_orchestrator import (
    CONVERSATION,
    StubClient,
    _agent,
    _build,
    _found,
    _handed_over,
    _reply,
)

SEARCH_LIMIT_TEXT = "already searched the knowledge base twice"


def _call(name: str, call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments={}, arguments_json="{}")


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(
            ToolDefinition(
                name=name,
                description="A tool.",
                parameters=(),
                handler=_handed_over if name == HANDOFF_TOOL else _found,
                terminal=name == HANDOFF_TOOL,
            )
        )
    return registry


class HonouringClient(StubClient):
    """A provider that honours `tool_choice`, as the real one does.

    Asked with tools open it searches again, every round; asked with none it
    answers. Which of the two the orchestrator gets is decided by what it asks.
    """

    def __init__(self) -> None:
        super().__init__([])
        self._searches = 0

    async def respond(self, **kwargs: Any) -> AgentReply:
        self.calls.append(kwargs)
        if kwargs.get("tool_choice") == "none":
            return _reply(text="Our branch opens at nine.")
        self._searches += 1
        return _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, f"search_{self._searches}")])


async def test_a_turn_that_keeps_searching_still_ends_in_a_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HonouringClient()
    orchestrator = _build(
        monkeypatch,
        client=as_http_client(client),
        agent=_agent(),
        grants=[SEARCH_KNOWLEDGE_TOOL],
        registry=_registry(SEARCH_KNOWLEDGE_TOOL),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply == "Our branch opens at nine."
    assert outcome.outcome is TurnOutcome.REPLIED
    assert outcome.rounds == 3
    assert [call.get("tool_choice") for call in client.calls] == [None, None, "none"]
    closing = client.calls[2]["turns"][-1]
    assert closing.role == "system" and "last step of this turn" in closing.text
    assert [spec.name for spec in client.calls[2]["tools"]] == [
        SEARCH_KNOWLEDGE_TOOL
    ], "the tools described stay the same, so the request prefix does"


async def test_words_said_alongside_a_tool_call_are_never_the_whole_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubClient(
        [
            _reply(text="Let me check.", tool_calls=[_call("lookup_order", "c1")]),
            _reply(text=None),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=as_http_client(client),
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry("lookup_order"),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply is None
    assert outcome.outcome is TurnOutcome.EMPTY_RESPONSE
    assert not outcome.composed


async def test_a_handoff_on_the_last_round_still_hands_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubClient(
        [
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, "s1")]),
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, "s2")]),
            _reply(text="A colleague will help.", tool_calls=[_call(HANDOFF_TOOL, "h1")]),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=as_http_client(client),
        agent=_agent(),
        grants=[SEARCH_KNOWLEDGE_TOOL, HANDOFF_TOOL],
        registry=_registry(SEARCH_KNOWLEDGE_TOOL, HANDOFF_TOOL),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.handed_off and outcome.reply is None
    assert outcome.outcome is TurnOutcome.HANDED_OFF
    assert client.calls[2]["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": HANDOFF_TOOL}],
    }


async def test_a_third_search_in_one_turn_is_refused_and_other_tools_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubClient(
        [
            _reply(
                tool_calls=[
                    _call(SEARCH_KNOWLEDGE_TOOL, "s1"),
                    _call(SEARCH_KNOWLEDGE_TOOL, "s2"),
                    _call(SEARCH_KNOWLEDGE_TOOL, "s3"),
                    _call("lookup_order", "o1"),
                ]
            ),
            _reply(text="Here is what I found."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=as_http_client(client),
        agent=_agent(),
        grants=[SEARCH_KNOWLEDGE_TOOL, "lookup_order"],
        registry=_registry(SEARCH_KNOWLEDGE_TOOL, "lookup_order"),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    outputs = [result.output for result in client.calls[1]["tool_results"]]
    assert outputs[:2] == ["found it", "found it"]
    assert SEARCH_LIMIT_TEXT in outputs[2]
    assert outputs[3] == "found it", "the cap is on searches, not on other tools"
    assert outcome.reply == "Here is what I found."


# ------------------------------------------------------------- transport order


def _body(*items: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_order",
            "output": list(items),
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        },
    )


def _said(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _asked(call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "lookup_order", "arguments": "{}"}


async def test_each_round_replays_the_previous_round_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = [
        _body(_said("Checking."), _asked("c1")),
        _body(_said("Still looking."), _asked("c2")),
        _body(_said("It shipped today.")),
    ]
    sent: list[dict[str, Any]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return scripted.pop(0)

    responses = ResponsesClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        api_key="sk-test-not-real",
        max_attempts=1,
    )
    orchestrator = _build(
        monkeypatch,
        client=as_http_client(responses),
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry("lookup_order"),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply == "It shipped today."
    first, second, third = (request["input"] for request in sent)
    assert second[: len(first)] == first
    assert third[: len(second)] == second, "round three begins with round two's input"

    def shape(item: dict[str, Any]) -> str:
        if item.get("type") in ("function_call", "function_call_output"):
            return f"{item['type']}:{item['call_id']}"
        return f"{item['role']}:{item['content']}"

    assert [shape(item) for item in third[:-1]] == [
        "user:hello",
        "assistant:Checking.",
        "function_call:c1",
        "function_call_output:c1",
        "assistant:Still looking.",
        "function_call:c2",
        "function_call_output:c2",
    ]
    assert third[-1]["role"] == "system"
    assert sent[2]["tool_choice"] == "none"
    assert [tool["name"] for tool in sent[2]["tools"]] == ["lookup_order"]
