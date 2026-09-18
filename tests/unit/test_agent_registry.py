"""Tool definitions and the validation of what a model sends back."""

import uuid
from typing import Any

import pytest

from app.agents.registry import (
    HANDOFF_TOOL,
    RECORD_LEAD_TOOL,
    SCHEDULE_FOLLOW_UP_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
    ToolArgumentError,
    ToolContext,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    build_default_registry,
    validate_arguments,
)
from app.db.models.tool_execution import ToolExecutionReason
from app.services.retrieval_service import MAX_TOP_K, effective_top_k
from tests.fakes import as_embeddings, as_session


async def _handler(context: ToolContext, arguments: dict[str, Any]) -> str:
    return "ran"


def _context() -> ToolContext:
    """A server-built context, which is the only kind a tool ever sees.

    These tests are about argument validation and the registry's refusals, so
    the identifiers are arbitrary - what matters is that a context exists at
    all, because the registry takes the workspace from it rather than from the
    model's arguments.
    """
    return ToolContext(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        # Never touched: every refusal these tests drive happens before a tool
        # body runs, which is the whole point of validating arguments first.
        session=as_session(None),
    )


def _definition(*parameters: ToolParameter) -> ToolDefinition:
    return ToolDefinition(
        name="lookup_order",
        description="Look up an order.",
        parameters=parameters,
        handler=_handler,
    )


def test_schema_lists_required_parameters_and_refuses_extras() -> None:
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
        ToolParameter(
            name="include_history",
            type="boolean",
            description="Include past orders.",
            required=False,
        ),
    )

    schema = definition.json_schema()

    assert schema["type"] == "object"
    assert schema["required"] == ["reference"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["include_history"]["type"] == "boolean"


def test_choices_become_an_enum() -> None:
    definition = _definition(
        ToolParameter(
            name="status",
            type="string",
            description="Order status.",
            choices=("open", "closed"),
        ),
    )

    schema = definition.json_schema()

    assert schema["properties"]["status"]["enum"] == ["open", "closed"]


def test_a_missing_required_argument_is_refused() -> None:
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {})


def test_an_invented_argument_is_refused() -> None:
    """Silence would hide an unclear description from whoever wrote the tool."""
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"reference": "A1", "colour": "red"})


def test_an_omitted_optional_argument_is_simply_absent() -> None:
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
        ToolParameter(
            name="note",
            type="string",
            description="Anything else.",
            required=False,
        ),
    )

    cleaned = validate_arguments(definition, {"reference": "A1", "note": None})

    assert cleaned == {"reference": "A1"}


def test_a_wrongly_typed_argument_is_refused() -> None:
    definition = _definition(
        ToolParameter(name="quantity", type="integer", description="How many."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"quantity": "three"})


def test_true_is_not_accepted_as_a_whole_number() -> None:
    """bool is an int in Python; accepting it would hide the model's mistake."""
    definition = _definition(
        ToolParameter(name="quantity", type="integer", description="How many."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"quantity": True})


def test_an_integer_is_accepted_where_a_number_is_wanted() -> None:
    definition = _definition(
        ToolParameter(name="amount", type="number", description="How much."),
    )

    cleaned = validate_arguments(definition, {"amount": 3})

    assert cleaned == {"amount": 3.0}


def test_a_value_outside_the_choices_is_refused() -> None:
    definition = _definition(
        ToolParameter(
            name="status",
            type="string",
            description="Order status.",
            choices=("open", "closed"),
        ),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"status": "pending"})


def test_registering_the_same_tool_twice_is_a_mistake() -> None:
    registry = ToolRegistry()
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )
    registry.register(definition)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)


def test_unknown_grants_are_skipped_rather_than_raised() -> None:
    """Grants outlive code: a removed tool must not break existing agents."""
    registry = build_default_registry()

    specs = registry.specs([HANDOFF_TOOL, "tool_from_a_future_release"])

    assert [spec.name for spec in specs] == [HANDOFF_TOOL]


async def test_calling_a_tool_that_does_not_exist_is_refused() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolArgumentError):
        await registry.run(name="nothing", arguments={}, context=_context())


async def test_a_registered_tool_runs_with_validated_arguments() -> None:
    registry = ToolRegistry()
    registry.register(
        _definition(
            ToolParameter(name="reference", type="string", description="Order reference."),
        )
    )

    output = await registry.run(
        name="lookup_order",
        arguments={"reference": "A1"},
        context=_context(),
    )

    assert output == "ran"


def test_the_default_registry_offers_the_expected_tools() -> None:
    """Asserted exhaustively on purpose.

    A tool appearing in the default registry is a capability every workspace can
    grant, so one arriving unnoticed is a change to the product, not a detail.
    """
    registry = build_default_registry()

    assert registry.knows(HANDOFF_TOOL)
    assert registry.knows(SEARCH_KNOWLEDGE_TOOL)
    assert registry.knows(RECORD_LEAD_TOOL)
    assert registry.knows(SCHEDULE_FOLLOW_UP_TOOL)
    # `names()` is sorted, so this reads alphabetically rather than by age.
    assert registry.names() == (
        RECORD_LEAD_TOOL,
        HANDOFF_TOOL,
        SCHEDULE_FOLLOW_UP_TOOL,
        SEARCH_KNOWLEDGE_TOOL,
    )


def test_the_follow_up_tool_offers_no_way_to_name_a_follow_up() -> None:
    """The nudge belongs to the conversation the turn is already in."""
    definition = build_default_registry().get(SCHEDULE_FOLLOW_UP_TOOL)

    assert definition is not None
    names = {parameter.name for parameter in definition.parameters}
    assert not names & {"follow_up_id", "conversation_id", "tenant_id"}


def test_the_follow_up_tool_needs_a_time_and_a_message() -> None:
    """Both required: a nudge with neither is not a nudge."""
    definition = build_default_registry().get(SCHEDULE_FOLLOW_UP_TOOL)

    assert definition is not None
    required = set(definition.json_schema()["required"])
    assert required == {"delay_minutes", "message"}


def test_recording_a_lead_asks_for_nothing_in_particular() -> None:
    """Every argument optional, by design.

    Extraction is partial: a customer gives their name in one message and their
    budget three messages later. A required field would either block the call or
    push the model into inventing a value to satisfy it.
    """
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    assert definition.json_schema()["required"] == []
    assert not any(parameter.required for parameter in definition.parameters)


def test_the_lead_tool_offers_no_way_to_name_a_lead() -> None:
    """The model reports what it heard; the service decides which lead that is.

    A lead id the model could pass is a lead id it could pass wrongly, and
    "wrongly" here includes another customer's record.
    """
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    names = {parameter.name for parameter in definition.parameters}
    assert not names & {"lead_id", "contact_id", "conversation_id", "tenant_id"}


def test_the_lead_tool_offers_no_way_to_set_judgement_fields() -> None:
    """Status, score and assignment are decisions, not extractions."""
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    names = {parameter.name for parameter in definition.parameters}
    assert not names & {"status", "score", "assigned_to_id", "tags"}


def test_the_budget_argument_is_a_number_not_prose() -> None:
    """ "500k" is ambiguous, so the schema does not invite it."""
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    budget = next(p for p in definition.parameters if p.name == "budget_amount")
    assert budget.type == "number"


# --------------------------------------------------------------- bounds (TOOL-16)
#
# The bounds the server enforces used to live only in the descriptions, in
# prose: "1 to 10", "1 to 43200". A model is told a limit in a sentence and
# violates it more often than one told in schema, and every violation is a
# wasted round at best - and, for `delay_minutes`, was the crash of TOOL-01.
# These assert the *serialized* schema, because that is the artefact the
# provider actually reads.


def test_the_published_schema_carries_the_bounds_the_server_enforces() -> None:
    registry = build_default_registry()

    search = registry.get(SEARCH_KNOWLEDGE_TOOL)
    follow_up = registry.get(SCHEDULE_FOLLOW_UP_TOOL)
    handoff = registry.get(HANDOFF_TOOL)
    lead = registry.get(RECORD_LEAD_TOOL)
    assert search is not None and follow_up is not None
    assert handoff is not None and lead is not None

    assert search.json_schema()["properties"]["max_results"]["minimum"] == 1
    assert search.json_schema()["properties"]["max_results"]["maximum"] == MAX_TOP_K

    delay = follow_up.json_schema()["properties"]["delay_minutes"]
    assert (delay["minimum"], delay["maximum"]) == (1, 43_200)
    assert follow_up.json_schema()["properties"]["message"]["maxLength"] == 4_096
    assert follow_up.json_schema()["properties"]["reason"]["maxLength"] == 300

    assert handoff.json_schema()["properties"]["reason"]["maxLength"] == 200
    assert lead.json_schema()["properties"]["budget_currency"]["pattern"] == "^[A-Z]{3}$"


def test_the_schema_still_refuses_arguments_nobody_declared() -> None:
    """The bound additions must not have loosened the object itself."""
    for name in build_default_registry().names():
        definition = build_default_registry().get(name)
        assert definition is not None
        assert definition.json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (SEARCH_KNOWLEDGE_TOOL, {"query": "prices", "max_results": 0}),
        (SEARCH_KNOWLEDGE_TOOL, {"query": "prices", "max_results": 11}),
        (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 0, "message": "hi"}),
        (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 43_201, "message": "hi"}),
        (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 10**15, "message": "hi"}),
        (HANDOFF_TOOL, {"reason": "r" * 201}),
        (RECORD_LEAD_TOOL, {"budget_currency": "egp"}),
        (RECORD_LEAD_TOOL, {"budget_amount": -1}),
    ],
)
def test_a_value_outside_a_published_bound_is_refused(tool: str, arguments: dict[str, Any]) -> None:
    definition = build_default_registry().get(tool)
    assert definition is not None

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, arguments)


def test_the_top_k_clamp_is_what_the_tool_actually_uses() -> None:
    """`effective_top_k` was tested directly; its use inside the tool was not (TM08)."""
    definition = build_default_registry().get(SEARCH_KNOWLEDGE_TOOL)
    assert definition is not None

    assert validate_arguments(definition, {"query": "q", "max_results": MAX_TOP_K}) == {
        "query": "q",
        "max_results": MAX_TOP_K,
    }
    assert effective_top_k(MAX_TOP_K + 5) == MAX_TOP_K


async def test_the_tool_clamps_a_count_even_when_nothing_validated_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler's own clamp, which is the second line and not the first (TM08).

    The published bound refuses an out-of-range count at the boundary, so no
    ordinary call can reach the clamp any more. It stays because the boundary is
    one layer and the service is another: a future caller that reaches the
    handler by some other route must still not be able to ask for four hundred
    passages. Driven by calling the handler directly with an argument
    `validate_arguments` would have refused - which is exactly the situation the
    clamp exists for.
    """
    import app.agents.registry as registry_module

    asked: dict[str, int] = {}

    class _Spy:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def search(self, *, query: str, top_k: int, **kwargs: Any) -> Any:
            asked["top_k"] = top_k

            class _Empty:
                passages: tuple[()] = ()

                def as_context(self) -> str:
                    return "nothing found"

            return _Empty()

    monkeypatch.setattr(registry_module, "RetrievalService", _Spy)

    definition = build_default_registry().get(SEARCH_KNOWLEDGE_TOOL)
    assert definition is not None
    context = ToolContext(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        session=as_session(object()),
        embeddings=as_embeddings(object()),
    )

    await definition.handler(context, {"query": "prices", "max_results": 400})

    assert asked["top_k"] == MAX_TOP_K


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (HANDOFF_TOOL, {"reason": "bad\x00reason"}),
        (RECORD_LEAD_TOOL, {"name": "Ah\x00med"}),
        (RECORD_LEAD_TOOL, {"interest": "flat \ud800"}),
        (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "hi\x00"}),
    ],
)
def test_text_the_database_cannot_hold_is_refused_at_the_boundary(
    tool: str, arguments: dict[str, Any]
) -> None:
    """NUL and lone surrogates are legal JSON and are not storable text (TOOL-01)."""
    definition = build_default_registry().get(tool)
    assert definition is not None

    with pytest.raises(ToolArgumentError) as refusal:
        validate_arguments(definition, arguments)
    assert refusal.value.reason is ToolExecutionReason.UNSAFE_TEXT


def test_arabic_rtl_marks_and_emoji_are_ordinary_text() -> None:
    """The control: the rule is about storability, never about alphabet."""
    definition = build_default_registry().get(RECORD_LEAD_TOOL)
    assert definition is not None

    arguments = {"name": "‏أحمد 😀", "interest": "تشطيب شقة 150م"}
    assert validate_arguments(definition, arguments) == arguments


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (HANDOFF_TOOL, {"reason": 7}),
        (RECORD_LEAD_TOOL, {"name": 7}),
        (RECORD_LEAD_TOOL, {"interest": ["a", "b"]}),
        (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": {"text": "hi"}}),
        (SEARCH_KNOWLEDGE_TOOL, {"query": None, "max_results": 3}),
    ],
)
def test_a_text_argument_that_is_not_text_is_refused(tool: str, arguments: dict[str, Any]) -> None:
    """String typing was only asserted where a type error also broke something else (TM06)."""
    definition = build_default_registry().get(tool)
    assert definition is not None

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, arguments)


# ------------------------------------------------ blank means absent (TM27)
#
# A blank optional lead field means "I learned nothing about this", never "clear
# what is stored". Making it a clear is a silent data-loss path from ordinary
# model output: a model that fills every argument on every call would erase the
# customer's name the first time it did not hear one.


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_lead_field_leaves_the_stored_value_alone(blank: str) -> None:
    from app.agents.registry import _optional_text

    assert _optional_text(blank) is None
    assert _optional_text(None) is None
    assert _optional_text("  Ahmed  ") == "Ahmed"


def test_the_handoff_tool_is_the_only_terminal_one() -> None:
    """Which tool may still run when no round is left to read a result (PD-TOOLS-05)."""
    registry = build_default_registry()
    terminal = {
        name for name in registry.names() if (d := registry.get(name)) is not None and d.terminal
    }
    assert terminal == {HANDOFF_TOOL}


def test_the_knowledge_tool_is_the_only_one_that_releases_the_session() -> None:
    """Which tool calls somebody else's API, and so must not sit in a savepoint (TOOL-09)."""
    registry = build_default_registry()
    releasing = {
        name
        for name in registry.names()
        if (d := registry.get(name)) is not None and d.releases_session
    }
    assert releasing == {SEARCH_KNOWLEDGE_TOOL}
