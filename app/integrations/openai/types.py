"""Internal request and response types for agent inference.

Services and the orchestrator depend on these rather than on provider payload
shapes (ADR-007). Everything that knows how OpenAI spells things stays inside
this package, so a provider change is absorbed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Self

from app.core.logging import get_logger

logger = get_logger(__name__)

Role = Literal["system", "user", "assistant"]

#: The largest token count one provider call can plausibly report (AI-11). The
#: biggest context any model this product uses offers is about a million tokens,
#: and output is capped far below that; a figure past two million is a provider,
#: proxy or parser gone wrong. It is refused here, at the boundary, because the
#: first place it would otherwise fail is a `BIGINT` usage column in the
#: transaction that commits a customer's reply - leaving the reply unsent and the
#: turn stranded.
MAX_REPORTED_TOKENS: Final = 2_000_000

_USAGE_FIELDS: Final = ("input_tokens", "output_tokens", "total_tokens")


def _count(value: object) -> int | None:
    """A provider-reported count this system can bill, or None if it is not one.

    Booleans are refused although Python counts them as ints, and so are
    negatives and anything past `MAX_REPORTED_TOKENS`. None means "not a usable
    count"; the caller decides what that becomes.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_REPORTED_TOKENS:
        return None
    return value


@dataclass(frozen=True, slots=True)
class Turn:
    """One message in the conversation as the model should see it.

    `images` are data URLs, and they are here rather than in a separate vision
    client so that describing a photograph reuses the retry, timeout and error
    handling the text path already has. A second client would be a second copy
    of that policy, drifting from this one.

    Data URLs rather than links: the alternative is putting every customer's
    attachment behind a URL a provider can reach, which is a far wider exposure
    than sending the bytes for one request.
    """

    role: Role
    text: str
    images: tuple[str, ...] = ()

    def to_input(self) -> dict[str, Any]:
        if not self.images:
            # Kept as a bare string when there is nothing else in the turn. The
            # provider accepts both, and this is what every existing turn sends.
            return {"role": self.role, "content": self.text}

        content: list[dict[str, Any]] = [{"type": "input_text", "text": self.text}]
        content.extend({"type": "input_image", "image_url": url} for url in self.images)
        return {"role": self.role, "content": content}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model is allowed to call on this turn.

    `parameters` is a JSON Schema object. It is supplied by the tool registry
    rather than assembled here, because the registry is what will validate the
    arguments that come back.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(frozen=True, slots=True)
class StructuredFormat:
    """A JSON shape the model is required to answer in.

    Used where the reply is read by code rather than by a person - a sentiment
    reading, not a message to a customer. Asking for JSON in the prompt and
    hoping is the alternative, and it fails on exactly the traffic that matters:
    the unusual message.

    `strict` is on by default, which the provider enforces. It comes with rules
    the schema must satisfy - every property listed in `required`, and
    `additionalProperties` false - so an optional field is expressed as a
    nullable type rather than by omission.
    """

    name: str
    schema: dict[str, Any]
    strict: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": self.name,
            "schema": self.schema,
            "strict": self.strict,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation the model asked for.

    `arguments_json` keeps the provider's original string alongside the decoded
    mapping. Submitting a tool result requires echoing the call back verbatim,
    and re-serialising a decoded dict is not guaranteed to reproduce it.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of running a tool, to be handed back to the model."""

    call_id: str
    name: str
    arguments_json: str
    output: str

    @classmethod
    def for_call(cls, call: ToolCall, *, output: str) -> Self:
        return cls(
            call_id=call.call_id,
            name=call.name,
            arguments_json=call.arguments_json,
            output=output,
        )

    def to_input(self) -> list[dict[str, Any]]:
        """Both halves of the exchange: the call, then its output.

        The call has to be resent because requests do not carry server-side
        state; without it the provider rejects an output for a call it has no
        record of.
        """
        return [
            {
                "type": "function_call",
                "call_id": self.call_id,
                "name": self.name,
                "arguments": self.arguments_json,
            },
            {
                "type": "function_call_output",
                "call_id": self.call_id,
                "output": self.output,
            },
        ]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one inference cost, for usage records and billing (Phase 12)."""

    input_tokens: int
    output_tokens: int
    total_tokens: int

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        """Read the provider's `usage` object, validated at the boundary (AI-11).

        Usage accounting must never be the reason a reply fails, so an absent,
        malformed or implausible count becomes zero rather than raising. An
        absent one is ordinary; one that is present and unusable is logged by
        field name, never by value, so a provider sending nonsense is visible.
        """
        if not isinstance(payload, dict):
            return cls(input_tokens=0, output_tokens=0, total_tokens=0)
        counts = {field: _count(payload.get(field)) for field in _USAGE_FIELDS}
        refused = [
            field
            for field in _USAGE_FIELDS
            if payload.get(field) is not None and counts[field] is None
        ]
        if refused:
            logger.warning(
                "openai.usage_implausible",
                extra={"event": "openai.usage_implausible", "fields": refused},
            )
        return cls(
            input_tokens=counts["input_tokens"] or 0,
            output_tokens=counts["output_tokens"] or 0,
            total_tokens=counts["total_tokens"] or 0,
        )


@dataclass(frozen=True, slots=True)
class AgentReply:
    """One completed inference.

    Text and tool calls are not exclusive: a model may explain itself and call a
    tool in the same turn, and dropping either half loses information the
    orchestrator needs.
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: TokenUsage
    response_id: str | None
    # The provider's own `usage` object and nothing more (AI-12). This used to
    # be the whole response body, held for the turn's lifetime with no reader -
    # a full customer conversation one careless log line away from a log. Kept
    # narrow for the one reader it has: the real-provider contract test proving
    # the fields the meter reads are really there.
    usage_payload: dict[str, Any] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_empty(self) -> bool:
        """Neither text nor a tool call: nothing to send and nothing to run."""
        return self.text is None and not self.tool_calls
