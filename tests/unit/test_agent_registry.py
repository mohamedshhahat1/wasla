"""Tool definitions and the validation of what a model sends back."""

import pytest

from app.agents.registry import (
    HANDOFF_TOOL,
    RECORD_LEAD_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
    ToolArgumentError,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    build_default_registry,
    validate_arguments,
)


async def _handler(context, arguments):
    return "ran"


def _definition(*parameters: ToolParameter):
    return ToolDefinition(
        name="lookup_order",
        description="Look up an order.",
        parameters=parameters,
        handler=_handler,
    )


def test_schema_lists_required_parameters_and_refuses_extras():
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


def test_choices_become_an_enum():
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


def test_a_missing_required_argument_is_refused():
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {})


def test_an_invented_argument_is_refused():
    """Silence would hide an unclear description from whoever wrote the tool."""
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"reference": "A1", "colour": "red"})


def test_an_omitted_optional_argument_is_simply_absent():
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


def test_a_wrongly_typed_argument_is_refused():
    definition = _definition(
        ToolParameter(name="quantity", type="integer", description="How many."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"quantity": "three"})


def test_true_is_not_accepted_as_a_whole_number():
    """bool is an int in Python; accepting it would hide the model's mistake."""
    definition = _definition(
        ToolParameter(name="quantity", type="integer", description="How many."),
    )

    with pytest.raises(ToolArgumentError):
        validate_arguments(definition, {"quantity": True})


def test_an_integer_is_accepted_where_a_number_is_wanted():
    definition = _definition(
        ToolParameter(name="amount", type="number", description="How much."),
    )

    cleaned = validate_arguments(definition, {"amount": 3})

    assert cleaned == {"amount": 3.0}


def test_a_value_outside_the_choices_is_refused():
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


def test_registering_the_same_tool_twice_is_a_mistake():
    registry = ToolRegistry()
    definition = _definition(
        ToolParameter(name="reference", type="string", description="Order reference."),
    )
    registry.register(definition)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)


def test_unknown_grants_are_skipped_rather_than_raised():
    """Grants outlive code: a removed tool must not break existing agents."""
    registry = build_default_registry()

    specs = registry.specs([HANDOFF_TOOL, "tool_from_a_future_release"])

    assert [spec.name for spec in specs] == [HANDOFF_TOOL]


async def test_calling_a_tool_that_does_not_exist_is_refused():
    registry = ToolRegistry()

    with pytest.raises(ToolArgumentError):
        await registry.run(name="nothing", arguments={}, context=None)


async def test_a_registered_tool_runs_with_validated_arguments():
    registry = ToolRegistry()
    registry.register(
        _definition(
            ToolParameter(name="reference", type="string", description="Order reference."),
        )
    )

    output = await registry.run(
        name="lookup_order",
        arguments={"reference": "A1"},
        context=None,
    )

    assert output == "ran"


def test_the_default_registry_offers_the_expected_tools():
    """Asserted exhaustively on purpose.

    A tool appearing in the default registry is a capability every workspace can
    grant, so one arriving unnoticed is a change to the product, not a detail.
    """
    registry = build_default_registry()

    assert registry.knows(HANDOFF_TOOL)
    assert registry.knows(SEARCH_KNOWLEDGE_TOOL)
    assert registry.knows(RECORD_LEAD_TOOL)
    # `names()` is sorted, so this reads alphabetically rather than by age.
    assert registry.names() == (RECORD_LEAD_TOOL, HANDOFF_TOOL, SEARCH_KNOWLEDGE_TOOL)


def test_recording_a_lead_asks_for_nothing_in_particular():
    """Every argument optional, by design.

    Extraction is partial: a customer gives their name in one message and their
    budget three messages later. A required field would either block the call or
    push the model into inventing a value to satisfy it.
    """
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    assert definition.json_schema()["required"] == []
    assert not any(parameter.required for parameter in definition.parameters)


def test_the_lead_tool_offers_no_way_to_name_a_lead():
    """The model reports what it heard; the service decides which lead that is.

    A lead id the model could pass is a lead id it could pass wrongly, and
    "wrongly" here includes another customer's record.
    """
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    names = {parameter.name for parameter in definition.parameters}
    assert not names & {"lead_id", "contact_id", "conversation_id", "tenant_id"}


def test_the_lead_tool_offers_no_way_to_set_judgement_fields():
    """Status, score and assignment are decisions, not extractions."""
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    names = {parameter.name for parameter in definition.parameters}
    assert not names & {"status", "score", "assigned_to_id", "tags"}


def test_the_budget_argument_is_a_number_not_prose():
    """ "500k" is ambiguous, so the schema does not invite it."""
    definition = build_default_registry().get(RECORD_LEAD_TOOL)

    assert definition is not None
    budget = next(p for p in definition.parameters if p.name == "budget_amount")
    assert budget.type == "number"
