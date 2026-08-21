"""The orchestrator loop, with a stubbed provider and fake repositories.

No database, no Redis, no HTTP. The orchestrator was deliberately given a client
and a registry rather than building its own, and this is what that buys.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import AgentOrchestrator
from app.agents.registry import HANDOFF_TOOL, ToolDefinition, ToolParameter, ToolRegistry
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.integrations.openai.types import AgentReply, TokenUsage, ToolCall

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
SENT_AT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class StubClient:
    """Hands out queued replies and records how it was asked."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def respond(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("The orchestrator asked for more replies than were queued.")
        return self._replies.pop(0)


class FakeConversations:
    def __init__(self, conversation):
        self._conversation = conversation

    async def require_by_id(self, conversation_id):
        return self._conversation


class FakeAgents:
    def __init__(self, agent):
        self._agent = agent

    async def get_answering_default(self):
        return self._agent

    async def get_by_id(self, agent_id):
        return self._agent


class FakeGrants:
    def __init__(self, names):
        self._names = tuple(names)

    async def list_for_agent(self, *, agent_id, enabled_only=True):
        return [SimpleNamespace(name=name) for name in self._names]


class FakeMessages:
    def __init__(self, messages):
        self._messages = list(messages)

    async def list_for_conversation(self, *, conversation_id, limit):
        return self._messages


def _returns(instance):
    def build(*args: object, **kwargs: object):
        return instance

    return build


async def _found(context, arguments):
    return "found it"


async def _handed_over(context, arguments):
    return "handed over"


def _agent(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Sales",
        "status": AgentStatus.ACTIVE,
        "model": "gpt-4.1-mini",
        "system_prompt": "Be helpful.",
        "temperature": 0.3,
        "max_output_tokens": None,
        "memory_message_limit": 20,
        "memory_token_budget": 4000,
        "is_default": True,
    }
    values.update(overrides)
    return Agent(**values)


def _inbound(body):
    return Message(
        direction=MessageDirection.INBOUND,
        status=MessageStatus.RECEIVED,
        kind=MessageKind.TEXT,
        body=body,
        created_at=SENT_AT,
    )


def _reply(text=None, tool_calls=(), tokens=10):
    return AgentReply(
        text=text,
        tool_calls=tuple(tool_calls),
        usage=TokenUsage(
            input_tokens=tokens,
            output_tokens=tokens,
            total_tokens=tokens * 2,
        ),
        response_id="resp_1",
        raw={},
    )


def _call(name, arguments=None):
    return ToolCall(
        call_id="call_1",
        name=name,
        arguments=arguments if arguments is not None else {},
        arguments_json="{}",
    )


def _registry_with(name, parameters=(), handler=None):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=name,
            description="A tool.",
            parameters=parameters,
            handler=handler if handler is not None else _found,
        )
    )
    return registry


def _build(
    monkeypatch,
    *,
    client,
    conversation=None,
    agent=None,
    messages=None,
    grants=(),
    registry=None,
    max_rounds=3,
):
    fakes = {
        "ConversationRepository": FakeConversations(
            conversation if conversation is not None else Conversation(mode=ConversationMode.AI)
        ),
        "AgentRepository": FakeAgents(agent),
        "AgentToolRepository": FakeGrants(grants),
        "MessageRepository": FakeMessages(
            messages if messages is not None else [_inbound("hello")]
        ),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(orchestrator_module, name, _returns(fake))

    return AgentOrchestrator(
        session=None,
        tenant_id=TENANT,
        client=client,
        registry=registry,
        max_rounds=max_rounds,
    )


async def test_a_conversation_a_human_owns_is_never_answered(monkeypatch):
    client = StubClient([])
    orchestrator = _build(
        monkeypatch,
        client=client,
        conversation=Conversation(mode=ConversationMode.HUMAN),
        agent=_agent(),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply is None
    assert not outcome.should_send
    assert client.calls == []


async def test_no_active_agent_means_no_reply(monkeypatch):
    client = StubClient([])
    orchestrator = _build(monkeypatch, client=client, agent=None)

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply is None
    assert client.calls == []


async def test_a_disabled_agent_does_not_answer(monkeypatch):
    client = StubClient([])
    disabled = _agent(status=AgentStatus.DISABLED)
    orchestrator = _build(monkeypatch, client=client, agent=disabled)

    outcome = await orchestrator.answer(conversation_id=CONVERSATION, agent=disabled)

    assert outcome.reply is None
    assert client.calls == []


async def test_an_empty_conversation_has_nothing_to_answer(monkeypatch):
    client = StubClient([])
    orchestrator = _build(monkeypatch, client=client, agent=_agent(), messages=[])

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply is None
    assert client.calls == []


async def test_a_plain_reply_is_returned_for_sending(monkeypatch):
    client = StubClient([_reply(text="Hello there.")])
    orchestrator = _build(monkeypatch, client=client, agent=_agent())

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply == "Hello there."
    assert outcome.should_send
    assert outcome.rounds == 1


async def test_the_agent_configuration_reaches_the_provider(monkeypatch):
    client = StubClient([_reply(text="Hello.")])
    agent = _agent(model="gpt-4.1", system_prompt="Be brief.", temperature=0.1)
    orchestrator = _build(monkeypatch, client=client, agent=agent)

    await orchestrator.answer(conversation_id=CONVERSATION)

    call = client.calls[0]
    assert call["model"] == "gpt-4.1"
    assert call["instructions"] == "Be brief."
    assert call["temperature"] == 0.1
    assert [turn.text for turn in call["turns"]] == ["hello"]


async def test_only_granted_tools_are_offered(monkeypatch):
    client = StubClient([_reply(text="Hello.")])
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry_with("lookup_order"),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    assert [spec.name for spec in client.calls[0]["tools"]] == ["lookup_order"]


async def test_a_tool_result_is_returned_to_the_model(monkeypatch):
    client = StubClient(
        [
            _reply(tool_calls=[_call("lookup_order")]),
            _reply(text="Your order is ready."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry_with("lookup_order"),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply == "Your order is ready."
    assert outcome.tools_run == ("lookup_order",)
    assert outcome.rounds == 2
    assert client.calls[1]["tool_results"][0].output == "found it"


async def test_text_said_alongside_a_tool_call_is_carried_forward(monkeypatch):
    """Tool results replay the call, not the words around it."""
    client = StubClient(
        [
            _reply(text="Let me check.", tool_calls=[_call("lookup_order")]),
            _reply(text="It is ready."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry_with("lookup_order"),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    second_round = [turn.text for turn in client.calls[1]["turns"]]
    assert second_round == ["hello", "Let me check."]


async def test_a_rejected_argument_becomes_output_the_model_can_read(monkeypatch):
    client = StubClient(
        [
            _reply(tool_calls=[_call("lookup_order")]),
            _reply(text="Sorry, which order?"),
        ]
    )
    registry = _registry_with(
        "lookup_order",
        parameters=(
            ToolParameter(name="reference", type="string", description="Order reference."),
        ),
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=registry,
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert "required" in client.calls[1]["tool_results"][0].output
    assert outcome.reply == "Sorry, which order?"


async def test_a_handoff_suppresses_the_reply(monkeypatch):
    """The conversation belongs to a person, so an AI message must not follow."""
    client = StubClient([_reply(text="Getting someone.", tool_calls=[_call(HANDOFF_TOOL)])])
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=[HANDOFF_TOOL],
        registry=_registry_with(HANDOFF_TOOL, handler=_handed_over),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.handed_off
    assert outcome.reply is None
    assert not outcome.should_send
    assert len(client.calls) == 1


async def test_the_round_limit_stops_the_loop_but_keeps_the_text(monkeypatch):
    client = StubClient(
        [
            _reply(text="Checking.", tool_calls=[_call("lookup_order")]),
            _reply(text="Still checking.", tool_calls=[_call("lookup_order")]),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry_with("lookup_order"),
        max_rounds=2,
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.rounds == 2
    assert outcome.reply == "Still checking."
    assert len(client.calls) == 2


async def test_usage_is_summed_across_rounds(monkeypatch):
    client = StubClient(
        [
            _reply(tool_calls=[_call("lookup_order")], tokens=10),
            _reply(text="Done.", tokens=5),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=["lookup_order"],
        registry=_registry_with("lookup_order"),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.usage.input_tokens == 15
    assert outcome.usage.total_tokens == 30
