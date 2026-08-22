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
from datetime import timedelta
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.db.models.analytics import AnalyticsSource
from app.db.models.conversation import ConversationMode
from app.db.models.lead import ActorKind
from app.integrations.openai.embeddings import EmbeddingsClient
from app.integrations.openai.types import ToolSpec
from app.services.follow_up_service import MAX_DELAY, MIN_DELAY, FollowUpService
from app.services.inbox_service import InboxService
from app.services.lead_service import ExtractedLead, LeadService
from app.services.retrieval_service import DEFAULT_TOP_K, MAX_TOP_K, RetrievalService

logger = get_logger(__name__)

ParameterType = Literal["string", "integer", "number", "boolean"]

HANDOFF_TOOL: Final = "request_human_handoff"
SEARCH_KNOWLEDGE_TOOL: Final = "search_knowledge"
RECORD_LEAD_TOOL: Final = "record_lead_details"
SCHEDULE_FOLLOW_UP_TOOL: Final = "schedule_follow_up"
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

    `embeddings` is optional because not every caller has a provider to hand -
    a test driving the handoff tool should not need one. A tool that requires it
    says so in its own output rather than failing the turn.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    session: AsyncSession
    embeddings: EmbeddingsClient | None = None


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
        # The agent asked for this one. A colleague taking a conversation over
        # and an agent giving up on it are the same row on `conversations` and
        # very different facts about the product.
        source=AnalyticsSource.AGENT,
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


async def _search_knowledge(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Look the question up in this workspace's own documents.

    Returns the passages as text, or an explicit statement that nothing was
    found. The empty answer is phrased as an instruction rather than left blank,
    because a model handed silence fills it from training data - which is
    exactly the invention grounding exists to prevent.
    """
    if context.embeddings is None:
        # Configuration is missing, not the model's mistake. Telling it so lets
        # it fall back to a handoff instead of retrying a tool that cannot work.
        logger.warning("agent.search_unavailable", extra={"tenant_id": str(context.tenant_id)})
        return (
            "The knowledge base cannot be searched right now. "
            "Do not guess an answer; offer to pass the question to a colleague."
        )

    query = str(arguments["query"])
    requested = arguments.get("max_results")
    top_k = int(requested) if isinstance(requested, int) else DEFAULT_TOP_K

    service = RetrievalService(
        session=context.session,
        # From the context, never from the arguments: a tenant id the model
        # could supply is a tenant id the model could change.
        tenant_id=context.tenant_id,
        embeddings=context.embeddings,
    )
    retrieval = await service.search(query=query, top_k=top_k)
    logger.info(
        "agent.knowledge_searched",
        extra={
            "conversation_id": str(context.conversation_id),
            "passages": len(retrieval.passages),
        },
    )
    return retrieval.as_context()


SEARCH_KNOWLEDGE_DEFINITION: Final = ToolDefinition(
    name=SEARCH_KNOWLEDGE_TOOL,
    description=(
        "Search the company's own documents for information before answering. "
        "Use it for any question about products, prices, policies, services or "
        "procedures. Answer only from what it returns; if it returns nothing, "
        "say you do not have that information."
    ),
    parameters=(
        ToolParameter(
            name="query",
            type="string",
            description=(
                "What to look up, in the customer's own words. Include the "
                "specific product, service or policy they asked about."
            ),
        ),
        ToolParameter(
            name="max_results",
            type="integer",
            description=(
                f"How many passages to return, 1 to {MAX_TOP_K}. " f"Defaults to {DEFAULT_TOP_K}."
            ),
            required=False,
        ),
    ),
    handler=_search_knowledge,
)


async def _record_lead_details(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Save what the customer said about themselves onto their lead.

    The model never names a lead. It reports what it learned, and the service
    resolves which lead that belongs to from the conversation's own contact.
    That is deliberate: a lead id the model could choose is a lead id the model
    could choose wrongly, and "wrongly" here includes another customer's record.

    It is also what makes the tool idempotent. Called five times across a
    conversation, it updates one lead five times rather than opening five.
    """
    service = LeadService(session=context.session, tenant_id=context.tenant_id)
    extracted = ExtractedLead(
        name=_optional_text(arguments.get("name")),
        phone=_optional_text(arguments.get("phone")),
        email=_optional_text(arguments.get("email")),
        interest=_optional_text(arguments.get("interest")),
        budget_amount=arguments.get("budget_amount"),
        budget_currency=_optional_text(arguments.get("budget_currency")),
    )
    if not extracted.as_fields():
        return (
            "Nothing was saved: no details were provided. "
            "Call this only once the customer has actually told you something."
        )

    try:
        lead = await service.capture_from_conversation(
            conversation_id=context.conversation_id,
            extracted=extracted,
        )
    except ConflictError:
        # The conversation was handed to a colleague between this job being
        # queued and it running. Phrased for the model, which must stop rather
        # than retry.
        return "A colleague has taken over this conversation. Do not reply further."

    logger.info(
        "agent.lead_recorded",
        extra={
            "conversation_id": str(context.conversation_id),
            "lead_id": str(lead.id),
        },
    )
    return (
        "The customer's details have been saved. "
        "Do not tell them about internal records; simply continue the conversation."
    )


def _optional_text(value: Any) -> str | None:
    """Treat blank text as absent, so an empty argument does not clear a field."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


RECORD_LEAD_DEFINITION: Final = ToolDefinition(
    name=RECORD_LEAD_TOOL,
    description=(
        "Save details the customer has given about themselves and what they "
        "want, so a colleague can follow up. Call it as soon as you learn a "
        "name, a contact detail, what they are interested in, or their budget. "
        "Send only what the customer actually said - never a guess, and never "
        "something you inferred from how they are writing."
    ),
    parameters=(
        ToolParameter(
            name="name",
            type="string",
            description="The customer's name, exactly as they gave it.",
            required=False,
        ),
        ToolParameter(
            name="phone",
            type="string",
            description=(
                "A phone number the customer gave for contact. Only include one "
                "if they stated it in the conversation."
            ),
            required=False,
        ),
        ToolParameter(
            name="email",
            type="string",
            description="An email address the customer gave.",
            required=False,
        ),
        ToolParameter(
            name="interest",
            type="string",
            description=(
                "One short sentence on what the customer wants, in their own "
                "terms - the product, service or job they described."
            ),
            required=False,
        ),
        ToolParameter(
            name="budget_amount",
            type="number",
            description=(
                "The budget the customer stated, as a plain number with no "
                "separators or currency symbol. Write 500000, not '500k' and "
                "not '500,000'. Omit it entirely unless they named a figure."
            ),
            required=False,
        ),
        ToolParameter(
            name="budget_currency",
            type="string",
            description="Three-letter currency code for the budget, such as EGP or USD.",
            required=False,
        ),
    ),
    handler=_record_lead_details,
)


# Expressed in minutes because that is the unit the model reasons in when a
# customer says "next week"; the service takes a timedelta.
MIN_FOLLOW_UP_MINUTES: Final = int(MIN_DELAY.total_seconds() // 60)
MAX_FOLLOW_UP_MINUTES: Final = int(MAX_DELAY.total_seconds() // 60)


async def _schedule_follow_up(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Arrange to say something later if the customer goes quiet.

    Like the lead tool, this names no record: the follow-up belongs to the
    conversation the turn is already in. Scheduling twice reschedules rather than
    queueing a second message, so a model that calls it on every turn cannot
    stack up notifications on one customer's phone.
    """
    minutes = int(arguments["delay_minutes"])
    message = str(arguments["message"]).strip()
    reason = _optional_text(arguments.get("reason"))

    service = FollowUpService(session=context.session, tenant_id=context.tenant_id)
    try:
        follow_up = await service.schedule(
            conversation_id=context.conversation_id,
            delay=timedelta(minutes=minutes),
            body=message,
            reason=reason,
            created_by_kind=ActorKind.AGENT,
        )
    except ValidationError as error:
        # Written for the model to read and correct on the next turn: a delay
        # outside the bounds, or a conversation that has since been closed.
        return f"The follow-up was not scheduled: {error}"

    logger.info(
        "agent.follow_up_scheduled",
        extra={
            "conversation_id": str(context.conversation_id),
            "follow_up_id": str(follow_up.id),
        },
    )
    return (
        "A follow-up has been scheduled. Do not mention it to the customer as a "
        "system action; if it is natural to say you will get back to them, say it "
        "in your own words."
    )


SCHEDULE_FOLLOW_UP_DEFINITION: Final = ToolDefinition(
    name=SCHEDULE_FOLLOW_UP_TOOL,
    description=(
        "Arrange to message the customer again later if they go quiet. Use it "
        "when they say they will think about it, ask you to check back, or leave "
        "a question open. Do not use it to send something now - just reply. "
        "Calling it again replaces the follow-up already waiting rather than "
        "adding a second, so it is safe to update as the conversation moves on."
    ),
    parameters=(
        ToolParameter(
            name="delay_minutes",
            type="integer",
            description=(
                "How long to wait before following up, in minutes "
                f"({MIN_FOLLOW_UP_MINUTES} to {MAX_FOLLOW_UP_MINUTES}). "
                "Use what the customer asked for: 1440 for tomorrow, 10080 for "
                "next week."
            ),
        ),
        ToolParameter(
            name="message",
            type="string",
            description=(
                "What to send when the time comes, written as you would say it "
                "to the customer, in the language they are using. Make it stand "
                "on its own - they may not remember this conversation."
            ),
        ),
        ToolParameter(
            name="reason",
            type="string",
            description=(
                "One short note for the colleague who reviews this later, "
                "explaining why a follow-up was appropriate."
            ),
            required=False,
        ),
    ),
    handler=_schedule_follow_up,
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
    registry.register(SEARCH_KNOWLEDGE_DEFINITION)
    registry.register(RECORD_LEAD_DEFINITION)
    registry.register(SCHEDULE_FOLLOW_UP_DEFINITION)
    return registry
