"""The AI writes each automatic follow-up from the conversation (FUS-04, ADR-151).

At dispatch, not at scheduling: the step is written from the conversation as it
is when it comes due. The model answers a fixed shape - `send` with the text, or
`skip` with a reason from a closed vocabulary - and is offered **no tools**: a
follow-up turn writes one message and nothing else (no order, no booking, no
handoff), because a nudge that could act would be an agent turn nobody asked
for, taken while the customer is silent.

**No lock and no transaction is held across the call** (ADR-093's rule for the
model): `FollowUpService.prepare_step` reads everything the request needs and
commits, the worker calls `StepWriter.write` with no session, and
`finish_step` takes the row again by its claim token - a reply, a takeover or
a restart in between cleared the token, and the text is discarded unsent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal, Protocol

from app.db.models.follow_up_sequence import AI_SKIP_REASONS, SequenceSkipReason
from app.integrations.openai.client import RESPOND_FOLLOW_UP, ResponsesClient
from app.integrations.openai.types import StructuredFormat, Turn

#: A follow-up is short; the ceiling bounds what one step may cost.
STEP_MAX_OUTPUT_TOKENS: Final = 400

_SKIP_VALUES: Final = sorted(reason.value for reason in AI_SKIP_REASONS)

#: The one shape the model may answer with (strict JSON schema).
STEP_FORMAT: Final = StructuredFormat(
    name="follow_up_step",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "skip"]},
            "reason": {"type": ["string", "null"], "enum": [*_SKIP_VALUES, None]},
            "text": {"type": "string"},
        },
        "required": ["action", "reason", "text"],
        "additionalProperties": False,
    },
)


class StepWriterError(Exception):
    """The model failed, timed out or answered outside the shape: no answer."""


@dataclass(frozen=True, slots=True)
class StepRequest:
    """Everything one step's model call needs, read before the transaction ended."""

    model: str
    instructions: str
    turns: tuple[Turn, ...]
    max_output_tokens: int = STEP_MAX_OUTPUT_TOKENS


@dataclass(frozen=True, slots=True)
class StepAnswer:
    """The model's decision: send `text`, or skip for `reason`."""

    action: Literal["send", "skip"]
    reason: SequenceSkipReason | None
    text: str


class StepWriter(Protocol):
    async def write(self, request: StepRequest) -> StepAnswer:
        """Write one step, or raise `StepWriterError`."""
        ...


class ModelStepWriter:
    """Writes a step with the Responses API, structured output, no tools."""

    def __init__(self, client: ResponsesClient) -> None:
        self._client = client

    async def write(self, request: StepRequest) -> StepAnswer:
        try:
            reply = await self._client.respond(
                model=request.model,
                instructions=request.instructions,
                turns=request.turns,
                max_output_tokens=request.max_output_tokens,
                response_format=STEP_FORMAT,
                operation=RESPOND_FOLLOW_UP,
            )
        except Exception as error:
            raise StepWriterError(type(error).__name__) from error
        return parse_answer(reply.text)


def parse_answer(text: str | None) -> StepAnswer:
    """The model's JSON as a `StepAnswer`; anything else is `StepWriterError`."""
    try:
        decoded: Any = json.loads(text or "")
    except ValueError as error:
        raise StepWriterError("not JSON") from error
    if not isinstance(decoded, dict):
        raise StepWriterError("not an object")
    action = decoded.get("action")
    body = decoded.get("text")
    reason = decoded.get("reason")
    if action not in ("send", "skip") or not isinstance(body, str):
        raise StepWriterError("outside the shape")
    if action == "skip":
        try:
            skip = SequenceSkipReason(reason) if reason else SequenceSkipReason.OTHER
        except ValueError:
            skip = SequenceSkipReason.OTHER
        if skip not in AI_SKIP_REASONS:
            skip = SequenceSkipReason.OTHER
        return StepAnswer(action="skip", reason=skip, text="")
    return StepAnswer(action="send", reason=None, text=body.strip())


def step_instructions(
    *,
    persona: str,
    step_index: int,
    steps_total: int,
    guidance: str | None,
    since_customer: timedelta,
    channel: str,
) -> str:
    """What the model is told for one step, after the agent's own instructions."""
    minutes = max(0, int(since_customer.total_seconds() // 60))
    lines = [
        persona.strip(),
        "",
        "## Follow-up",
        f"The customer has not answered for {minutes} minutes. Write follow-up "
        f"{step_index} of {steps_total} on {channel}: one short message that picks up "
        "exactly what this conversation was about, in the language and dialect the "
        "customer used. Do not repeat earlier messages word for word, do not invent "
        "offers, prices or facts that are not in the conversation, and do not ask more "
        "than one question.",
        "Answer `skip` instead of writing, with the reason, when a follow-up would not "
        "make sense: the customer concluded or said goodbye (concluded), bought or said "
        "they will buy elsewhere or in person (purchased), declined (declined), is "
        "waiting for the business to do something (waiting_on_business), or a message "
        "now would be inappropriate (inappropriate); otherwise use other.",
        "Answer only with the JSON object: action, reason (null when sending), text "
        "(empty when skipping).",
    ]
    if guidance:
        lines.insert(4, f"The business asks for this follow-up: {guidance}")
    return "\n".join(lines)


__all__ = [
    "STEP_FORMAT",
    "STEP_MAX_OUTPUT_TOKENS",
    "ModelStepWriter",
    "StepAnswer",
    "StepRequest",
    "StepWriter",
    "StepWriterError",
    "parse_answer",
    "step_instructions",
]
