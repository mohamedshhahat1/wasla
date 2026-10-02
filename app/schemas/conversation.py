"""Conversation and message API contracts.

Read models are mapped field by field rather than inferred from the ORM object,
so adding a column to a table never silently widens the API.

Conversations carry identifiers rather than embedded contact objects. The models
declare no ORM relationships on purpose: a lazy load inside an async request is
blocking I/O that only shows up under load.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.channels.policy import (
    ChannelState,
    OutOfWindow,
    ReplyPolicy,
    SendMechanism,
    TextUnit,
)
from app.db.models.channel import Channel, ContactIdentity, IdentityKind
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
    ReplyActionSource,
)
from app.db.models.sentiment import ConversationPriority, SentimentLabel
from app.schemas.bounds import TEMPLATE_COMPONENTS, check_json
from app.schemas.text import StorableText
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS

# Meta's own limit for a text body, taken from the service that enforces it
# rather than restated here. Two copies of a provider's limit drift, and the
# copy that drifts is the one nobody is testing: the schema rejects early and
# politely, `MessagingService.send_text` is the guarantee, and they have to be
# the same number for the first to mean anything (MSG-25).
MAX_TEXT_LENGTH = WHATSAPP_TEXT_MAX_CHARS


class SendTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: StorableText = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    preview_url: bool = False


class SendTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StorableText = Field(min_length=1, max_length=512)
    language: StorableText = Field(min_length=2, max_length=16)
    # Forwarded to Meta, whose shape this deliberately does not model. Bounded
    # so an oversized structure is refused here rather than after a database
    # write and a Graph API round trip - see `app.schemas.bounds`.
    components: list[dict[str, Any]] | None = None

    @field_validator("components")
    @classmethod
    def _bounded_components(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None:
            check_json(value, TEMPLATE_COMPONENTS, field="components")
        return value


class ModeUpdateRequest(BaseModel):
    """Take a conversation over, or give it back to the AI.

    Taking over an AI conversation makes the caller its owner and records
    `handoff_reason`. On a conversation already human it changes nothing - use
    the assignment endpoint to move it between colleagues (PD-CRM-3, PD-CRM-5).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ConversationMode
    handoff_reason: StorableText | None = Field(default=None, max_length=200)


class PriorityUpdateRequest(BaseModel):
    """Set the priority by hand.

    The only way it comes down. Assessment raises it and never lowers it, so
    returning a conversation to the ordinary queue is a decision somebody makes
    after looking at it.
    """

    model_config = ConfigDict(extra="forbid")

    priority: ConversationPriority


class AssignmentRequest(BaseModel):
    """Give a conversation to a colleague, or clear its owner.

    `expected_assigned_to_id` is required, and null is a value: it is who the
    caller believes owns the conversation now - `assigned_to_id` from the
    `ConversationRead` they are looking at. The write happens only if that is
    still the owner; otherwise the answer is 409 `stale_assignment` and nothing
    changes (PD-CRM-4). Last-writer-wins let two colleagues both be told they
    owned a customer, and let a stale screen undo a manager's reassignment.
    """

    model_config = ConfigDict(extra="forbid")

    # Null clears the assignment. A value must be an active member here.
    assigned_to_id: uuid.UUID | None = None
    expected_assigned_to_id: uuid.UUID | None


class CursorPage[ItemT](BaseModel):
    """One page, and the cursor that asks for the next.

    `next_cursor` is null when the collection is exhausted. Clients should stop
    on that rather than counting items: a full page is not proof of more, and an
    empty one is not proof of none once filtering is involved.
    """

    items: list[ItemT]
    next_cursor: str | None = None


class ReplyActionRead(BaseModel):
    """What a customer tapped (OMNI-030): the provider's id or payload, and its words."""

    id_or_payload: str | None
    title: str | None
    source: ReplyActionSource


class MessageRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    # The provider's id for this message, whatever the channel (OMNI-015).
    provider_message_id: str | None
    wa_message_id: str | None = Field(
        deprecated=(
            "Use provider_message_id, which carries the same value on every "
            "channel. Kept until clients have moved (docs/API.md)."
        ),
    )
    direction: MessageDirection
    kind: MessageKind
    status: MessageStatus
    # What produced this line. Exposed because it is the question a reader of
    # the transcript has, and because inferring it from `sent_by_id` gets
    # campaigns and follow-ups wrong (MSG-16).
    origin: MessageOrigin
    body: str | None
    # Set when the customer tapped a button or a list row rather than typing:
    # `body` carries its words, this carries the payload a client routes on.
    # Null on everything else. Additive (OMNI-030).
    action: ReplyActionRead | None = None
    # Set on template messages only, so a client can render which template went
    # out in place of the text it has no copy of.
    template_name: str | None
    template_language: str | None
    sent_by_id: uuid.UUID | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    failure_reason: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, message: Message) -> Self:
        return cls(
            id=message.id,
            conversation_id=message.conversation_id,
            provider_message_id=message.wa_message_id,
            wa_message_id=message.wa_message_id,
            direction=message.direction,
            kind=message.kind,
            status=message.status,
            origin=message.origin,
            body=message.body,
            action=(
                ReplyActionRead(
                    id_or_payload=message.action_payload,
                    title=message.action_title,
                    source=message.action_source,
                )
                if message.action_source is not None
                else None
            ),
            template_name=message.template_name,
            template_language=message.template_language,
            sent_by_id=message.sent_by_id,
            sent_at=message.sent_at,
            delivered_at=message.delivered_at,
            read_at=message.read_at,
            failure_reason=message.failure_reason,
            created_at=message.created_at,
        )


class ParticipantRead(BaseModel):
    """Who a conversation is with, as its channel addresses them - never the value itself.

    The identifier (a phone number, a business-scoped id) is personal data and
    is not needed to render an inbox; `kind` says what sort of address it is, so
    a client can say "username" rather than showing an empty phone field.
    """

    id: uuid.UUID
    channel: Channel
    kind: IdentityKind

    @classmethod
    def from_model(cls, identity: ContactIdentity) -> Self:
        return cls(id=identity.id, channel=identity.channel, kind=identity.kind)


class ReplyPolicyRead(BaseModel):
    """What a person may send on this conversation now (ADR-121).

    The channel's rule, stated rather than inferred: whether free text is
    allowed and until when, what may be sent after that, and the text limit in
    the channel's own unit - characters on WhatsApp, UTF-8 bytes on a channel
    that counts bytes.
    """

    free_text_allowed: bool
    window_expires_at: datetime | None
    out_of_window: OutOfWindow
    templates: bool
    text_limit: int
    text_limit_unit: TextUnit
    # `operational`, or `paused` / `unavailable` when Wasla cannot act on the
    # channel: then nothing is sendable, whatever the window (OMNI-031).
    # Additive; a client that ignores it still reads `free_text_allowed` and
    # `templates` as false.
    state: ChannelState = ChannelState.OPERATIONAL
    # Per origin (OMNI-033), additive. How a person's free text would go now -
    # `standard_window`, or `human_agent_tag` after the window on a channel
    # that has one - and whether an agent's may go at all, which a human-agent
    # tag never allows.
    free_text_mechanism: SendMechanism | None = None
    agent_free_text_allowed: bool = False

    @classmethod
    def from_policy(cls, policy: ReplyPolicy) -> Self:
        return cls(
            free_text_allowed=policy.free_text_allowed,
            window_expires_at=policy.window_expires_at,
            out_of_window=policy.out_of_window,
            templates=policy.templates,
            text_limit=policy.text_limit,
            text_limit_unit=policy.text_limit_unit,
            state=policy.state,
            free_text_mechanism=policy.free_text_mechanism,
            agent_free_text_allowed=policy.agent_free_text_allowed,
        )


class ConversationRead(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    # The connection this conversation is on. Kept under its first name; the
    # name was always neutral, and `connection_id` carries the same value.
    account_id: uuid.UUID
    connection_id: uuid.UUID
    channel: Channel
    participant: ParticipantRead
    status: ConversationStatus
    mode: ConversationMode
    assigned_to_id: uuid.UUID | None
    handoff_reason: str | None
    last_message_at: datetime | None
    last_inbound_at: datetime | None
    # The latest reading of how the customer sounds, and what it implied.
    # `priority` is what an inbox sorts on; the rest explains why it is there.
    sentiment: SentimentLabel | None
    sentiment_score: float | None
    priority: ConversationPriority
    intent: str | None
    intent_confidence: float | None
    # Whether the channel's standard free-form window is open, so a client can
    # disable its composer instead of discovering the rule by failing a send.
    # Its meaning never widens: `reply_policy` is where a channel's other rules
    # are stated (ADR-121).
    service_window_open: bool
    reply_policy: ReplyPolicyRead
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        conversation: Conversation,
        *,
        service_window_open: bool,
        reply_policy: ReplyPolicy,
        participant: ContactIdentity,
    ) -> Self:
        return cls(
            id=conversation.id,
            contact_id=conversation.contact_id,
            account_id=conversation.account_id,
            connection_id=conversation.account_id,
            channel=conversation.channel,
            participant=ParticipantRead.from_model(participant),
            status=conversation.status,
            mode=conversation.mode,
            assigned_to_id=conversation.assigned_to_id,
            handoff_reason=conversation.handoff_reason,
            last_message_at=conversation.last_message_at,
            last_inbound_at=conversation.last_inbound_at,
            sentiment=conversation.sentiment,
            sentiment_score=conversation.sentiment_score,
            priority=conversation.priority,
            intent=conversation.intent,
            intent_confidence=conversation.intent_confidence,
            service_window_open=service_window_open,
            reply_policy=ReplyPolicyRead.from_policy(reply_policy),
            created_at=conversation.created_at,
        )
