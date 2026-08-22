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

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.sentiment import ConversationPriority, SentimentLabel

# Meta's own limit for a text body.
MAX_TEXT_LENGTH = 4096


class SendTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    preview_url: bool = False


class SendTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    language: str = Field(min_length=2, max_length=16)
    components: list[dict[str, Any]] | None = None


class ModeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ConversationMode
    handoff_reason: str | None = Field(default=None, max_length=200)


class PriorityUpdateRequest(BaseModel):
    """Set the priority by hand.

    The only way it comes down. Assessment raises it and never lowers it, so
    returning a conversation to the ordinary queue is a decision somebody makes
    after looking at it.
    """

    model_config = ConfigDict(extra="forbid")

    priority: ConversationPriority


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null clears the assignment. A value must belong to this workspace.
    assigned_to_id: uuid.UUID | None = None


class CursorPage[ItemT](BaseModel):
    """One page, and the cursor that asks for the next.

    `next_cursor` is null when the collection is exhausted. Clients should stop
    on that rather than counting items: a full page is not proof of more, and an
    empty one is not proof of none once filtering is involved.
    """

    items: list[ItemT]
    next_cursor: str | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    wa_message_id: str | None
    direction: MessageDirection
    kind: MessageKind
    status: MessageStatus
    body: str | None
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
            wa_message_id=message.wa_message_id,
            direction=message.direction,
            kind=message.kind,
            status=message.status,
            body=message.body,
            template_name=message.template_name,
            template_language=message.template_language,
            sent_by_id=message.sent_by_id,
            sent_at=message.sent_at,
            delivered_at=message.delivered_at,
            read_at=message.read_at,
            failure_reason=message.failure_reason,
            created_at=message.created_at,
        )


class ConversationRead(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    account_id: uuid.UUID
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
    # Whether free-form messages are still allowed, so a client can disable its
    # composer instead of discovering the rule by failing a send.
    service_window_open: bool
    created_at: datetime

    @classmethod
    def from_model(cls, conversation: Conversation, *, service_window_open: bool) -> Self:
        return cls(
            id=conversation.id,
            contact_id=conversation.contact_id,
            account_id=conversation.account_id,
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
            created_at=conversation.created_at,
        )
