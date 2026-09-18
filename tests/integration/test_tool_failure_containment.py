"""A tool that breaks loses its own work, and nothing else (TOOL-01, TOOL-02).

The four tools shipped today are careful: the registry refuses text the database
cannot store and numbers outside the published bounds before a handler runs, and
the lead and follow-up services contain their own integrity errors in savepoints
of their own. That is the right shape, and it has an awkward consequence for
testing — *no real tool can currently raise an unexpected exception*, so the
executor's last two defences have nothing to defend against.

They are still the difference between a lost call and a lost customer. Before
this remediation, a NUL in a handoff reason or a concurrent duplicate raised
inside a handler, escaped `_execute`, and ended the turn: no reply, no handoff,
no explanation, and the earlier rounds' writes still committed. Both of those
specific causes are gone; the class is not, and the next tool somebody writes
will not have been designed by anybody who read this file.

So these run a registry of their own, containing one tool that does exactly what
a careless handler does: stages a row and then raises. What is asserted is that
the turn survives, the customer is answered, the broken tool's row is gone, and
the tool that ran before it kept its work.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents.registry import (
    RECORD_LEAD_TOOL,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    build_default_registry,
)
from app.core.exceptions import ValidationError
from app.db.models.follow_up import FollowUp
from app.db.models.tool_execution import ToolExecutionReason, ToolExecutionState
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

ANSWER = "Noted."
BREAKING_TOOL = "stage_then_break"
WRITTEN_BODY = "A row a careless tool staged."


class _Careless:
    """A handler that writes and then raises, which is the whole point.

    `writes` says whether it stages a row first. With it, the savepoint is what
    is under test; without it, the final containment layer is.
    """

    def __init__(self, *, writes: bool) -> None:
        self._writes = writes
        self.calls = 0

    async def __call__(self, context: ToolContext, arguments: dict[str, Any]) -> str:
        self.calls += 1
        if self._writes:
            context.session.add(
                FollowUp(
                    tenant_id=context.tenant_id,
                    conversation_id=context.conversation_id,
                    scheduled_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
                    + __import__("datetime").timedelta(hours=1),
                    body=WRITTEN_BODY,
                )
            )
            await context.session.flush()
        raise RuntimeError("a careless tool " + uuid.uuid4().hex[:6])


def _registry_with(handler: _Careless) -> ToolRegistry:
    """The deployment's own tools, plus one that misbehaves."""
    registry = build_default_registry()
    registry.register(
        ToolDefinition(
            name=BREAKING_TOOL,
            description="Stages a row and then raises.",
            parameters=(
                ToolParameter(
                    name="note",
                    type="string",
                    description="Anything at all.",
                    required=False,
                    max_length=100,
                ),
            ),
            handler=handler,
        )
    )
    return registry


async def test_a_tool_that_raises_after_writing_loses_its_row_and_not_the_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The savepoint. Without it the broken tool's row commits with everyone else's."""
    handler = _Careless(writes=True)
    ai_turns.registry = _registry_with(handler)
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL, BREAKING_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (RECORD_LEAD_TOOL, {"name": "Ahmed"}),
                (BREAKING_TOOL, {"note": "go on then"}),
            )
        ),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the careless handler really ran, and really staged its row
    # before raising.
    assert handler.calls == 1

    # Its work is gone, and the tool that ran before it kept its own.
    assert [row.body for row in await ai_turns.follow_ups(workspace.tenant_id)] == []
    assert [lead.name for lead in await ai_turns.leads(workspace.tenant_id)] == ["Ahmed"]

    # The customer was answered, which is the finding.
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied"]
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]

    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (RECORD_LEAD_TOOL, ToolExecutionState.SUCCEEDED.value, None),
        (
            BREAKING_TOOL,
            ToolExecutionState.FAILED.value,
            ToolExecutionReason.INTERNAL_ERROR.value,
        ),
    ]


async def test_a_tool_that_simply_raises_does_not_end_the_customers_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The final containment layer, with nothing staged to complicate it."""
    handler = _Careless(writes=False)
    ai_turns.registry = _registry_with(handler)
    workspace = await ai_turns.workspace(grants=[BREAKING_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(
        tool_calls_response(((BREAKING_TOOL, {"note": "go on then"}),)),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert handler.calls == 1
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == ["replied"]
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (
            BREAKING_TOOL,
            ToolExecutionState.FAILED.value,
            ToolExecutionReason.INTERNAL_ERROR.value,
        )
    ]


async def test_what_the_model_reads_after_a_broken_tool_says_nothing_about_it(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A fixed sentence, never the exception: it could carry SQL or a customer's values."""
    handler = _Careless(writes=False)
    ai_turns.registry = _registry_with(handler)
    workspace = await ai_turns.workspace(grants=[BREAKING_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(
        tool_calls_response(((BREAKING_TOOL, {"note": "go on then"}),)),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    outputs = [
        item["output"]
        for item in ai_providers.agent_requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    assert "a careless tool" not in outputs[0]
    assert "RuntimeError" not in outputs[0]
    assert "did not work" in outputs[0]


async def _refuses_on_domain_grounds(context: ToolContext, arguments: dict[str, Any]) -> str:
    raise ValidationError(DOMAIN_REFUSAL)


DOMAIN_REFUSAL = "That order is already closed."
REFUSING_TOOL = "refuse_on_domain_grounds"


async def test_a_domain_refusal_reaches_the_model_in_its_own_words(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A rule the domain enforces is something the model can act on (TM14).

    The catch-all added for TOOL-01 would contain a `WaslaError` too - and give
    the model a fixed sentence that says only that something broke. A domain
    refusal is not a break: "that order is already closed" tells the model what
    to say next, and it is recorded as `domain_error` rather than lumped in with
    exceptions nobody anticipated.
    """
    registry = build_default_registry()
    registry.register(
        ToolDefinition(
            name=REFUSING_TOOL,
            description="Refuses on domain grounds.",
            parameters=(),
            handler=_refuses_on_domain_grounds,
        )
    )
    ai_turns.registry = registry
    workspace = await ai_turns.workspace(grants=[REFUSING_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["cancel my order"])
    ai_providers.agent = scripted(
        tool_calls_response(((REFUSING_TOOL, {}),)),
        text_response(ANSWER),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    (output,) = [
        item["output"]
        for item in ai_providers.agent_requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert output == "That did not work: " + DOMAIN_REFUSAL
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (
            REFUSING_TOOL,
            ToolExecutionState.FAILED.value,
            ToolExecutionReason.DOMAIN_ERROR.value,
        )
    ]
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
