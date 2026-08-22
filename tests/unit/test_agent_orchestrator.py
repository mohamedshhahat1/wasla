"""The orchestrator loop, with a stubbed provider and fake repositories.

No database, no Redis, no HTTP. The orchestrator was deliberately given a client
and a registry rather than building its own, and this is what that buys.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import AgentOrchestrator
from app.agents.registry import (
    HANDOFF_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    build_default_registry,
)
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
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


class FakeMedia:
    """The attachments on a conversation, keyed by message id.

    Empty by default: most of these tests are about text, and a conversation
    with no files must render exactly as it did before media existed.
    """

    def __init__(self, attachments=None):
        self._attachments = attachments or {}

    async def map_for_messages(self, message_ids):
        return {
            message_id: self._attachments[message_id]
            for message_id in message_ids
            if message_id in self._attachments
        }


def _returns(instance):
    def build(*args: object, **kwargs: object):
        return instance

    return build


async def _found(context, arguments):
    return "found it"


async def _handed_over(context, arguments):
    return "handed over"


async def _empty_search(context, arguments):
    """What the real tool returns when the knowledge base holds nothing."""
    return (
        "No information about this was found in the company's knowledge base. "
        "Tell the customer you do not have that information rather than guessing, "
        "and offer to pass the question to a colleague."
    )


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
    embeddings=None,
    attachments=None,
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
        "MediaRepository": FakeMedia(attachments),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(orchestrator_module, name, _returns(fake))

    return AgentOrchestrator(
        session=None,
        tenant_id=TENANT,
        client=client,
        registry=registry,
        max_rounds=max_rounds,
        embeddings=embeddings,
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


# --- Grounding: what the agent does with retrieved knowledge -----------------


def _search_registry(handler):
    """A registry holding only search_knowledge, backed by `handler`."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=SEARCH_KNOWLEDGE_TOOL,
            description="Search the company documents.",
            parameters=(
                ToolParameter(name="query", type="string", description="What to look up."),
            ),
            handler=handler,
        )
    )
    return registry


async def test_the_agent_can_actually_invoke_search_knowledge(monkeypatch):
    """The whole chain: a granted tool, a call, and a handler that ran."""
    searched = []

    async def fake_search(context, arguments):
        searched.append(arguments)
        return "Premium finishing costs 7200 EGP per square metre."

    client = StubClient(
        [
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, {"query": "premium finishing"})]),
            _reply(text="Premium finishing is 7200 EGP per square metre."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(SEARCH_KNOWLEDGE_TOOL,),
        registry=_search_registry(fake_search),
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert searched == [{"query": "premium finishing"}]
    assert outcome.tools_run == (SEARCH_KNOWLEDGE_TOOL,)
    assert outcome.reply == "Premium finishing is 7200 EGP per square metre."


async def test_a_granted_search_tool_is_offered_to_the_model(monkeypatch):
    """A granted tool must reach the provider payload, or it can never be called.

    Uses the real default registry, so a tool renamed or dropped from it fails
    here rather than silently going missing from every agent.
    """
    client = StubClient([_reply(text="hello")])
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(SEARCH_KNOWLEDGE_TOOL,),
        registry=build_default_registry(),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    assert SEARCH_KNOWLEDGE_TOOL in [spec.name for spec in client.calls[0]["tools"]]


async def test_an_agent_without_the_grant_is_never_offered_it(monkeypatch):
    """Tools are granted per agent; a booking agent need not read the price list."""
    client = StubClient([_reply(text="hello")])
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(HANDOFF_TOOL,),
        registry=build_default_registry(),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    assert SEARCH_KNOWLEDGE_TOOL not in [spec.name for spec in client.calls[0]["tools"]]


async def test_retrieved_knowledge_reaches_the_next_provider_call(monkeypatch):
    """Retrieval is worthless if the passages never enter the model's context."""
    passage = "Economy finishing costs 4500 EGP per square metre."

    async def fake_search(context, arguments):
        return passage

    client = StubClient(
        [
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, {"query": "economy finishing"})]),
            _reply(text="It is 4500 EGP per square metre."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(SEARCH_KNOWLEDGE_TOOL,),
        registry=_search_registry(fake_search),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    outputs = [result.output for result in client.calls[1]["tool_results"]]
    assert passage in outputs


async def test_an_empty_retrieval_reaches_the_model_as_an_instruction(monkeypatch):
    """The agent must be told there was nothing, not handed silence.

    A model given a blank tool result fills the gap from its training data,
    which is exactly the invented answer grounding exists to prevent.
    """
    client = StubClient(
        [
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, {"query": "warranty"})]),
            _reply(text="I do not have that information, but I can ask a colleague."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(SEARCH_KNOWLEDGE_TOOL,),
        registry=_search_registry(_empty_search),
    )

    await orchestrator.answer(conversation_id=CONVERSATION)

    output = client.calls[1]["tool_results"][0].output
    assert output.strip() != ""
    assert "do not have that information" in output.lower()


async def test_a_search_without_a_provider_says_so_rather_than_failing(monkeypatch):
    """Missing configuration is ours, not the model's mistake.

    Saying so plainly lets it fall back to a handoff instead of retrying a tool
    that cannot work. Uses the real registry, so this exercises the real
    handler's guard.
    """
    client = StubClient(
        [
            _reply(tool_calls=[_call(SEARCH_KNOWLEDGE_TOOL, {"query": "prices"})]),
            _reply(text="Let me pass you to a colleague."),
        ]
    )
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        grants=(SEARCH_KNOWLEDGE_TOOL,),
        registry=build_default_registry(),
        embeddings=None,
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    output = client.calls[1]["tool_results"][0].output
    assert "cannot be searched" in output.lower()
    assert "do not guess" in output.lower()
    assert outcome.reply == "Let me pass you to a colleague."


async def test_an_image_description_reaches_the_model(monkeypatch):
    """End to end through the orchestrator, not just the renderer.

    This is the assertion the whole phase exists for: what the customer
    photographed has to arrive in the prompt. Before media understanding the
    model saw the literal string "[image]" and answered as though nothing had
    been sent.
    """
    photo = Message(
        id=uuid.uuid4(),
        direction=MessageDirection.INBOUND,
        status=MessageStatus.RECEIVED,
        kind=MessageKind.IMAGE,
        body="how much?",
        created_at=SENT_AT,
    )
    attachment = MessageMedia(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        message_id=photo.id,
        conversation_id=CONVERSATION,
        status=MediaStatus.READY,
        transcript="A blue sofa with a price tag reading 4,500 EGP.",
        is_voice=False,
        byte_size=0,
        attempts=0,
    )

    client = StubClient([_reply("It is 4,500 EGP.")])
    orchestrator = _build(
        monkeypatch,
        client=client,
        agent=_agent(),
        messages=[photo],
        attachments={photo.id: attachment},
    )

    outcome = await orchestrator.answer(conversation_id=CONVERSATION)

    assert outcome.reply == "It is 4,500 EGP."
    prompt = " ".join(turn.text for turn in client.calls[0]["turns"])
    assert "4,500 EGP" in prompt
    assert "how much?" in prompt
