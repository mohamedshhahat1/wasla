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


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null clears the assignment. A value must belong to this workspace.
    assigned_to_id: uuid.UUID | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    wa_message_id: str | None
    direction: MessageDirection
    kind: MessageKind
    status: MessageStatus
    body: str | None
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
            service_window_open=service_window_open,
            created_at=conversation.created_at,
        )
