"""The tools an agent may call, and validation of what the model sends back.

A tool is a name, a schema and a handler. Parameters are declared with a small
internal type rather than raw JSON Schema because the same declaration does two
jobs: it describes the tool to the model and it checks the arguments that come
back. Two hand-written copies of one contract drift apart.

There is no JSON Schema validator in the dependency set, and adding one to check
four scalar types would be disproportionate. These checks cover exactly what the
parameter type can express and refuse anything it cannot.

**Bounds are part of the declaration** (TOOL-01, TOOL-16). A parameter carries
its own length, range and pattern, which are published in the provider schema
*and* enforced here. Publishing them is a courtesy to the model - a bound stated
in prose is violated more often than one stated in schema - and enforcing them is
the contract: the provider's adherence is advisory, this is what holds. Before
this, every bound lived downstream in a service, so `delay_minutes` of 10^15 was
accepted at the boundary and raised `OverflowError` inside `timedelta`, and a
model that emitted a NUL or a lone surrogate in any text argument killed the
customer's turn at the database.

**Text safety is applied here** (TOOL-01). `app.core.text_safety` already refuses
what PostgreSQL cannot store and was applied on the knowledge-upload path only;
tool arguments are the other place text a stranger influenced reaches a column.
Valid Arabic, RTL marks and emoji pass untouched - the rule is about what can be
stored, not about what alphabet it is in.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.text_safety import storable_problem
from app.db.models.analytics import AnalyticsSource
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.conversation import Conversation, ConversationMode
from app.db.models.follow_up import MAX_BODY_LENGTH, MAX_REASON_LENGTH
from app.db.models.lead import ActorKind
from app.db.models.tool_execution import ToolExecutionReason
from app.integrations.openai.embeddings import EmbeddingsClient
from app.integrations.openai.types import ToolSpec
from app.services.audit_service import AuditTrail
from app.services.follow_up_service import MAX_DELAY, MIN_DELAY, FollowUpService
from app.services.inbox_service import InboxService
from app.services.lead_service import (
    MAX_BUDGET,
    MAX_EMAIL_LENGTH,
    MAX_INTEREST_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PHONE_LENGTH,
    ExtractedLead,
    LeadService,
)
from app.services.retrieval_service import (
    DEFAULT_TOP_K,
    MAX_QUERY_CHARACTERS,
    MAX_TOP_K,
    RetrievalService,
    effective_top_k,
)

logger = get_logger(__name__)

ParameterType = Literal["string", "integer", "number", "boolean"]

HANDOFF_TOOL: Final = "request_human_handoff"
SEARCH_KNOWLEDGE_TOOL: Final = "search_knowledge"
RECORD_LEAD_TOOL: Final = "record_lead_details"
SCHEDULE_FOLLOW_UP_TOOL: Final = "schedule_follow_up"
# Conversation.handoff_reason is String(200); a longer reason would fail at the
# database rather than at the model.
MAX_HANDOFF_REASON_LENGTH: Final = 200


MIN_TOP_K: Final = 1
# Currency codes are three upper-case letters and nothing else. Published to
# the model as a pattern so it is told the shape rather than guessing it.
CURRENCY_PATTERN: Final = "^[A-Z]{3}$"


class ToolArgumentError(Exception):
    """The model called a tool with arguments it cannot use.

    Deliberately not a domain exception. Domain exceptions carry HTTP statuses,
    and this one must never become a response: the orchestrator turns it into
    tool output so the model can correct itself on the next turn.

    `reason` is which *kind* of refusal it was, from the closed vocabulary the
    execution record stores. The message and the reason exist for different
    readers and neither does the other's job: "delay_minutes must be at most
    43200" is what a model needs to correct itself and is useless in a
    `GROUP BY`; `range_violation` is what an operator counts and is useless to
    the model.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: ToolExecutionReason = ToolExecutionReason.INVALID_ARGUMENTS,
    ) -> None:
        super().__init__(message)
        self.reason = reason


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

    The bounds are declared once and used twice, exactly as the type is: they
    become `minimum`/`maximum`/`minLength`/`maxLength`/`pattern` in the schema
    the provider is given, and they are what `validate_arguments` enforces. A
    bound present in one and absent from the other is the drift this class
    exists to prevent (TOOL-16).
    """

    name: str
    type: ParameterType
    description: str
    required: bool = True
    choices: tuple[str, ...] | None = None
    #: Inclusive bounds for `integer` and `number`.
    minimum: int | float | None = None
    maximum: int | float | None = None
    #: Inclusive bounds on the length of a `string`, in characters.
    min_length: int | None = None
    max_length: int | None = None
    #: A regular expression the whole string must match. JSON Schema's `pattern`
    #: is a search rather than a full match, so every pattern used here anchors
    #: itself and `re.fullmatch` is what enforces it.
    pattern: str | None = None

    def constraints(self) -> dict[str, Any]:
        """The bounds as JSON Schema spells them."""
        schema: dict[str, Any] = {}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.min_length is not None:
            schema["minLength"] = self.min_length
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.pattern is not None:
            schema["pattern"] = self.pattern
        return schema


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool the platform implements."""

    name: str
    description: str
    parameters: tuple[ToolParameter, ...]
    handler: ToolHandler
    #: Whether this tool ends the turn by itself. A terminal tool is the one
    #: thing still worth doing when the model can no longer read a result, so it
    #: alone runs on the final round (PD-TOOLS-05), and it stops the rest of its
    #: own response (PD-TOOLS-02).
    terminal: bool = False
    #: Whether the handler hands the session's connection back while it calls
    #: somebody else's API. A tool that does must not be wrapped in a savepoint
    #: by the executor: releasing commits, and a savepoint open across the
    #: release would defeat it (TOOL-09, ADR-080). Such a tool contains its own
    #: database failures, which `search_knowledge` has always done.
    releases_session: bool = False

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
            # Published, not merely described. A limit a model is told in prose
            # is violated more often than one it is told in schema, and every
            # violation is a wasted round at best (TOOL-16).
            schema.update(parameter.constraints())
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
        _checked_range(parameter, value)
        return value

    if parameter.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolArgumentError(f"Argument {parameter.name} must be a number.")
        _checked_range(parameter, value)
        return float(value)

    if not isinstance(value, str):
        raise ToolArgumentError(f"Argument {parameter.name} must be text.")
    if parameter.choices and value not in parameter.choices:
        allowed = ", ".join(parameter.choices)
        raise ToolArgumentError(f"Argument {parameter.name} must be one of: {allowed}.")
    return _checked_text(parameter, value)


def _checked_range(parameter: ToolParameter, value: int | float) -> None:
    """Refuse a number outside the bound the schema publishes.

    **Before the value is used for anything**, which is the whole point. The
    follow-up service bounded a delay at thirty days and did it by building
    `timedelta(minutes=…)` and adding it to `now` - so 10^15 minutes raised
    `OverflowError` on the way to the check that would have refused it, and the
    customer's turn died (TOOL-01). A bound applied after the value has been
    converted is not a bound.
    """
    if parameter.minimum is not None and value < parameter.minimum:
        raise ToolArgumentError(
            f"Argument {parameter.name} must be at least {parameter.minimum}.",
            reason=ToolExecutionReason.RANGE_VIOLATION,
        )
    if parameter.maximum is not None and value > parameter.maximum:
        raise ToolArgumentError(
            f"Argument {parameter.name} must be at most {parameter.maximum}.",
            reason=ToolExecutionReason.RANGE_VIOLATION,
        )


def _checked_text(parameter: ToolParameter, value: str) -> str:
    """Refuse text the server cannot store, or that breaks a published bound.

    Storability first, because it is the failure that used to escape: a NUL or a
    lone surrogate is legal JSON, legal in a Python string, and refused by
    PostgreSQL at flush - after the tool had already staged its write (TOOL-01).
    Refused here it is an ordinary tool rejection the model reads and corrects.

    Nothing is stripped or repaired. A model that wrote a NUL is told so, for
    the same reason the upload path tells an API client rather than quietly
    sanitising what somebody submitted. Ordinary Unicode is untouched: Arabic,
    RTL marks and emoji are all storable text and all pass.
    """
    problem = storable_problem(value)
    if problem is not None:
        raise ToolArgumentError(
            f"Argument {parameter.name} {problem}.",
            reason=ToolExecutionReason.UNSAFE_TEXT,
        )
    if parameter.min_length is not None and len(value) < parameter.min_length:
        raise ToolArgumentError(
            f"Argument {parameter.name} must be at least " f"{parameter.min_length} characters.",
            reason=ToolExecutionReason.RANGE_VIOLATION,
        )
    if parameter.max_length is not None and len(value) > parameter.max_length:
        raise ToolArgumentError(
            f"Argument {parameter.name} must be at most {parameter.max_length} characters.",
            reason=ToolExecutionReason.RANGE_VIOLATION,
        )
    if parameter.pattern is not None and not re.fullmatch(parameter.pattern, value):
        raise ToolArgumentError(
            f"Argument {parameter.name} is not in the expected format.",
            reason=ToolExecutionReason.RANGE_VIOLATION,
        )
    return value


def _record(
    context: ToolContext,
    action: AuditAction,
    *,
    target_type: str,
    target_id: uuid.UUID,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write one audit row for something an agent actually did (ADR-052).

    Three rules, and each closes a way the trail could lie.

    **The actor is fixed.** `AuditActorKind.AGENT` is passed literally and no
    `actor` user is supplied, so nothing a model emits can influence who the
    row says acted. There is no argument through which to claim otherwise: the
    model's output reaches this function only as `meta`, never as identity.

    **The scope comes from the context.** `tenant_id` and the conversation are
    read from the server-built `ToolContext`, which the orchestrator assembled
    from the worker's job - not from tool arguments. A compromised model cannot
    write a row into another workspace's trail because it cannot name one.

    **Only facts, never content.** `meta` carries shapes and identifiers - which
    fields were filled, how long a delay was - and never the customer's words,
    the agent's prompt or a handoff reason. An audit log is read by people
    investigating an incident; it must not become a second copy of the
    conversation, and a trail that quietly accumulated message text would be a
    privacy problem of its own.

    Called only after the mutation has succeeded. Every caller sits below its
    service call, past the branches that return early on refusal, so a refused
    tool cannot leave a row claiming it ran.
    """
    entry: dict[str, Any] = {"conversation_id": str(context.conversation_id)}
    if meta:
        entry.update(meta)
    AuditTrail(context.session, tenant_id=context.tenant_id).record(
        action,
        actor_kind=AuditActorKind.AGENT,
        target_type=target_type,
        target_id=target_id,
        tenant_id=context.tenant_id,
        meta=entry,
    )


async def _request_human_handoff(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Switch the conversation to human mode and stop answering it.

    **A conversation a person already owns is left alone** (TOOL-07,
    PD-TOOLS-01). Handing over something already handed over is not a second
    handoff; it is a model with a stale picture of the world writing over a
    colleague's own note about why they took the conversation. Before this it
    did exactly that: the colleague's "VIP, call personally" became the model's
    sentence, an agent handoff audit row was written for a handover the agent
    did not perform, and the analytics counter that exists to report handovers
    counted one conversation twice.

    Read as a column rather than through the repository, and read here as well
    as in the executor's own lifecycle gate: the executor is the guard, this is
    the one the service keeps if a future caller arrives by another route.
    """
    mode = await context.session.scalar(
        select(Conversation.mode).where(
            Conversation.id == context.conversation_id,
            Conversation.tenant_id == context.tenant_id,
        )
    )
    if mode is ConversationMode.HUMAN:
        logger.info(
            "agent.handoff_already_human",
            extra={
                "event": "agent.handoff_already_human",
                "conversation_id": str(context.conversation_id),
            },
        )
        # Raised rather than returned, so the executor records that no handoff
        # happened. A string here would read as a successful handoff and the
        # turn would be filed as `handed_off` - claiming the agent did
        # something a colleague had already done.
        raise ConflictError("This conversation is already handled by a colleague.")

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
    # After the mode change, never before: a handoff that raised must not leave
    # a row saying the conversation was handed over. The reason itself is not
    # recorded here - it is a sentence about a customer, it is already on the
    # conversation row, and the trail answers "who and when", not "what was
    # said".
    _record(
        context,
        AuditAction.AGENT_HANDOFF_REQUESTED,
        target_type="conversation",
        target_id=context.conversation_id,
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
            min_length=1,
            # The column's own width, published rather than silently applied.
            # It used to be a truncation the model was never told about, so a
            # long reason lost its ending and nothing said so (TM09).
            max_length=MAX_HANDOFF_REASON_LENGTH,
        ),
    ),
    handler=_request_human_handoff,
    # Handing over is the end of the turn by definition, which is what makes it
    # the one tool still worth running when no round is left to read a result.
    terminal=True,
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
    # Bounded by the service whatever the model asked for (M07): a count the
    # model chooses is a count the model can make enormous.
    top_k = effective_top_k(arguments.get("max_results"))

    service = RetrievalService(
        session=context.session,
        # From the context, never from the arguments: a tenant id the model
        # could supply is a tenant id the model could change.
        tenant_id=context.tenant_id,
        embeddings=context.embeddings,
    )
    # No threshold or context size is taken from the arguments at all; both
    # are the server's (M27). A failed search raises
    # `KnowledgeSearchUnavailableError`, which the orchestrator gives the model
    # as a failed tool call rather than ending the customer's turn (RAG-03).
    # The connection goes back to the pool for the embedding call (TOOL-09,
    # ADR-080). A knowledge search that follows a write in the same model
    # response used to hold a pooled connection, an open transaction and a
    # `RowExclusiveLock` on `leads` for the whole of an embeddings round trip -
    # up to three attempts with backoff - which is precisely the bottleneck
    # ADR-080 removed from the inference path, reappearing inside a tool. It
    # also blocked the concurrent turn TOOL-02 is about.
    #
    # Releasing commits, so the previous tool's finished work becomes durable
    # here. That is the price and it is the right one: the alternative is
    # holding a write lock across somebody else's API.
    retrieval = await service.search(query=query, top_k=top_k, release_session=True)
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
            min_length=1,
            max_length=MAX_QUERY_CHARACTERS,
        ),
        ToolParameter(
            name="max_results",
            type="integer",
            description=(
                f"How many passages to return, {MIN_TOP_K} to {MAX_TOP_K}. "
                f"Defaults to {DEFAULT_TOP_K}."
            ),
            required=False,
            # The clamp the service applies, published. `effective_top_k` still
            # runs and is still what holds; this is what stops the model asking
            # for four hundred in the first place.
            minimum=MIN_TOP_K,
            maximum=MAX_TOP_K,
        ),
    ),
    handler=_search_knowledge,
    # Its own provider call, made with the session released (TOOL-09). The
    # executor must not wrap it in a savepoint; it contains its own database
    # failures in a nested transaction of its own.
    releases_session=True,
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
        capture = await service.capture_from_conversation(
            conversation_id=context.conversation_id,
            extracted=extracted,
        )
    except ConflictError:
        # The conversation was handed to a colleague between this job being
        # queued and it running. Phrased for the model, which must stop rather
        # than retry.
        return "A colleague has taken over this conversation. Do not reply further."

    # Which fields the agent filled, never what it wrote into them. "The agent
    # set a phone number on this lead at 14:02" is the auditable fact; the
    # number itself is the customer's personal data and belongs on the lead
    # row alone, where deleting the lead deletes it.
    #
    # **Only when something changed** (TOOL-18). A chatty model calling this on
    # every turn with the same details used to write a row per call - forty
    # calls in one response produced forty rows - burying the mutations the
    # trail exists to show under repeats of one. The service already knows
    # which fields actually moved; a row now says that one of them did.
    if capture.changed_fields:
        _record(
            context,
            AuditAction.AGENT_LEAD_RECORDED,
            target_type="lead",
            target_id=capture.lead.id,
            meta={"fields": sorted(capture.changed_fields)},
        )
    logger.info(
        "agent.lead_recorded",
        extra={
            "conversation_id": str(context.conversation_id),
            "lead_id": str(capture.lead.id),
            "changed": len(capture.changed_fields),
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
            # The column's own width. Blank still means "absent" rather than
            # "clear this" - see `_optional_text` - so there is no minimum.
            max_length=MAX_NAME_LENGTH,
        ),
        ToolParameter(
            name="phone",
            type="string",
            description=(
                "A phone number the customer gave for contact. Only include one "
                "if they stated it in the conversation."
            ),
            required=False,
            max_length=MAX_PHONE_LENGTH,
        ),
        ToolParameter(
            name="email",
            type="string",
            description="An email address the customer gave.",
            required=False,
            max_length=MAX_EMAIL_LENGTH,
        ),
        ToolParameter(
            name="interest",
            type="string",
            description=(
                "One short sentence on what the customer wants, in their own "
                "terms - the product, service or job they described."
            ),
            required=False,
            max_length=MAX_INTEREST_LENGTH,
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
            # A budget is not negative and not larger than the column. The
            # service still validates the scale and refuses NaN; this is what
            # the model is told.
            minimum=0,
            maximum=float(MAX_BUDGET),
        ),
        ToolParameter(
            name="budget_currency",
            type="string",
            description="Three-letter currency code for the budget, such as EGP.",
            required=False,
            min_length=3,
            max_length=3,
            pattern=CURRENCY_PATTERN,
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
    except ConflictError:
        # A colleague owns the conversation. Planting an AI nudge underneath
        # them is the one thing handing a conversation over is supposed to stop
        # (TOOL-06), so this is a refusal rather than a schedule.
        return "A colleague has taken over this conversation. Do not reply further."
    except ValidationError as error:
        # Written for the model to read and correct on the next turn: a delay
        # outside the bounds, or a conversation that has since been closed.
        return f"The follow-up was not scheduled: {error}"

    # The delay is a scheduling fact and is recorded; the message body is text
    # that will be sent to a customer and is not.
    _record(
        context,
        AuditAction.AGENT_FOLLOW_UP_SCHEDULED,
        target_type="follow_up",
        target_id=follow_up.id,
        meta={"delay_minutes": minutes},
    )
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
            # Checked before anything builds a `timedelta` out of it. The
            # service's own bound was applied after `now + timedelta(minutes=…)`
            # and so was never reached for a number big enough to overflow
            # (TOOL-01).
            minimum=MIN_FOLLOW_UP_MINUTES,
            maximum=MAX_FOLLOW_UP_MINUTES,
        ),
        ToolParameter(
            name="message",
            type="string",
            description=(
                "What to send when the time comes, written as you would say it "
                "to the customer, in the language they are using. Make it stand "
                "on its own - they may not remember this conversation."
            ),
            min_length=1,
            max_length=MAX_BODY_LENGTH,
        ),
        ToolParameter(
            name="reason",
            type="string",
            description=(
                "One short note for the colleague who reviews this later, "
                "explaining why a follow-up was appropriate."
            ),
            required=False,
            max_length=MAX_REASON_LENGTH,
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
