"""The tools an agent may call, and validation of what the model sends back.

A tool is a name, a schema and a handler. Parameters are declared with a small
internal type rather than raw JSON Schema because the same declaration does two
jobs: it describes the tool to the model and it checks the arguments that come
back. Two hand-written copies of one contract drift apart.

There is no JSON Schema validator in the dependency set, and adding one to check
four scalar types would be disproportionate. These checks cover exactly what the
parameter type can express and refuse anything it cannot.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.conversation import ConversationMode
from app.integrations.openai.types import ToolSpec
from app.services.inbox_service import InboxService

logger = get_logger(__name__)

ParameterType = Literal["string", "integer", "number", "boolean"]

HANDOFF_TOOL: Final = "request_human_handoff"
# Conversation.handoff_reason is String(200); a longer reason would fail at the
# database rather than at the model.
MAX_HANDOFF_REASON_LENGTH: Final = 200


class ToolArgumentError(Exception):
    """The model called a tool with arguments it cannot use.

    Deliberately not a domain exception. Domain exceptions carry HTTP statuses,
    and this one must never become a response: the orchestrator turns it into
    tool output so the model can correct itself on the next turn.
    """


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool is allowed to know about where it was called.

    The tenant id is passed explicitly rather than inferred, so a tool cannot
    accidentally act outside the workspace whose conversation triggered it.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    session: AsyncSession


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ToolParameter:
    """One argument a tool accepts.

    `description` is prompt text: it is the only explanation the model gets, so
    it is part of the contract rather than a comment.
    """

    name: str
    type: ParameterType
    description: str
    required: bool = True
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool the platform implements."""

    name: str
    description: str
    parameters: tuple[ToolParameter, ...]
    handler: ToolHandler

    def json_schema(self) -> dict[str, Any]:
        """The parameters as the provider expects to see them."""
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []

        for parameter in self.parameters:
            schema: dict[str, Any] = {
                "type": parameter.type,
                "description": parameter.description,
            }
            if parameter.choices:
                schema["enum"] = list(parameter.choices)
            properties[parameter.name] = schema
            if parameter.required:
                required.append(parameter.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            # Refused rather than ignored: an invented argument means the
            # description is unclear, and silence would hide that.
            "additionalProperties": False,
        }

    def to_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.json_schema(),
        )


def validate_arguments(
    definition: ToolDefinition,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Check arguments against a definition, returning the usable ones.

    Every message raised here is written for the model to read: it is fed back
    as the tool's output, and a vague message produces the same mistake again.
    """
    expected = {parameter.name: parameter for parameter in definition.parameters}

    unexpected = sorted(set(arguments) - set(expected))
    if unexpected:
        raise ToolArgumentError("Unexpected arguments: " + ", ".join(unexpected) + ".")

    cleaned: dict[str, Any] = {}
    for name, parameter in expected.items():
        value = arguments.get(name)
        if value is None:
            if parameter.required:
                raise ToolArgumentError(f"Argument {name} is required.")
            continue
        cleaned[name] = _checked_value(parameter, value)
    return cleaned


def _checked_value(parameter: ToolParameter, value: Any) -> Any:
    if parameter.type == "boolean":
        if not isinstance(value, bool):
            raise ToolArgumentError(f"Argument {parameter.name} must be true or false.")
        return value

    if parameter.type == "integer":
        # bool is an int in Python, but a model sending true here meant a
        # boolean, so accepting it would hide the mistake.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolArgumentError(f"Argument {parameter.name} must be a whole number.")
        return value

    if parameter.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolArgumentError(f"Argument {parameter.name} must be a number.")
        return float(value)

    if not isinstance(value, str):
        raise ToolArgumentError(f"Argument {parameter.name} must be text.")
    if parameter.choices and value not in parameter.choices:
        allowed = ", ".join(parameter.choices)
        raise ToolArgumentError(f"Argument {parameter.name} must be one of: {allowed}.")
    return value


async def _request_human_handoff(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Switch the conversation to human mode and stop answering it."""
    reason = str(arguments["reason"])[:MAX_HANDOFF_REASON_LENGTH]
    inbox = InboxService(session=context.session, tenant_id=context.tenant_id)
    await inbox.set_mode(
        conversation_id=context.conversation_id,
        mode=ConversationMode.HUMAN,
        handoff_reason=reason,
    )
    logger.info(
        "agent.handoff_requested",
        extra={"conversation_id": str(context.conversation_id)},
    )
    return "This conversation has been handed to a colleague. Do not reply further."


HANDOFF_DEFINITION: Final = ToolDefinition(
    name=HANDOFF_TOOL,
    description=(
        "Hand this conversation to a human colleague and stop replying. Use it "
        "when the customer asks for a person, is angry or distressed, or asks "
        "something you cannot answer from the information you have."
    ),
    parameters=(
        ToolParameter(
            name="reason",
            type="string",
            description="One short sentence for the colleague taking over.",
        ),
    ),
    handler=_request_human_handoff,
)


class ToolRegistry:
    """The tools this deployment implements.

    A workspace grants tools by name, so the registry is the only thing that
    decides what those names actually do.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"The tool {definition.name} is already registered.")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def knows(self, name: str) -> bool:
        return name in self._definitions

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def specs(self, names: Iterable[str]) -> list[ToolSpec]:
        """Describe the named tools to the model, skipping any it cannot call.

        An unknown name is logged and dropped rather than raised. Grants outlive
        code: removing a tool must not stop every agent that was granted it.
        """
        specs: list[ToolSpec] = []
        for name in names:
            definition = self._definitions.get(name)
            if definition is None:
                logger.warning("agent.tool_not_implemented", extra={"tool": name})
                continue
            specs.append(definition.to_spec())
        return specs

    async def run(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> str:
        """Validate and run one call, returning output for the model."""
        definition = self._definitions.get(name)
        if definition is None:
            raise ToolArgumentError(f"There is no tool named {name}.")
        return await definition.handler(context, validate_arguments(definition, arguments))


def build_default_registry() -> ToolRegistry:
    """The tools every workspace can grant today.

    Built fresh rather than shared as a module-level singleton, so a test that
    registers a stub cannot leak it into the next test.
    """
    registry = ToolRegistry()
    registry.register(HANDOFF_DEFINITION)
    return registry
