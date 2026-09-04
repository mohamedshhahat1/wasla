"""Follow-up API contracts.

The create request accepts either a delay or an absolute time, and exactly one
of them. Both are genuinely useful — "in half an hour" is how a person thinks
during a conversation, "on Tuesday at nine" is how they think when planning —
and accepting both at once would leave the service picking a winner silently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.follow_up import (
    MAX_BODY_LENGTH,
    MAX_REASON_LENGTH,
    FollowUp,
    FollowUpStatus,
)
from app.db.models.lead import ActorKind
from app.schemas.bounds import TEMPLATE_COMPONENTS, check_json
from app.services.follow_up_service import MAX_DELAY, MIN_DELAY

MIN_DELAY_MINUTES = int(MIN_DELAY.total_seconds() // 60)
MAX_DELAY_MINUTES = int(MAX_DELAY.total_seconds() // 60)


class FollowUpCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    delay_minutes: int | None = Field(
        default=None,
        ge=MIN_DELAY_MINUTES,
        le=MAX_DELAY_MINUTES,
    )
    scheduled_at: datetime | None = None

    body: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    template_name: str | None = Field(default=None, max_length=512)
    template_language: str | None = Field(default=None, min_length=2, max_length=16)
    # Forwarded to Meta when the follow-up fires, so it is bounded on the way
    # in rather than on the way out - see `app.schemas.bounds`.
    template_components: list[dict[str, Any]] | None = None

    @field_validator("template_components")
    @classmethod
    def _bounded_components(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None:
            check_json(value, TEMPLATE_COMPONENTS, field="template_components")
        return value

    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    lead_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_time(self) -> Self:
        if (self.delay_minutes is None) == (self.scheduled_at is None):
            raise ValueError("Supply exactly one of delay_minutes or scheduled_at.")
        return self

    @model_validator(mode="after")
    def _something_to_send(self) -> Self:
        """Rejected here as well as in the service.

        The service is the guarantee — the agent tool reaches it without passing
        through this schema — but catching it here turns a 422 with a field path
        into the answer, rather than a generic domain error.
        """
        if not self.body and not self.template_name:
            raise ValueError("Supply a body, a template, or both.")
        return self


class FollowUpCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class FollowUpRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    lead_id: uuid.UUID | None
    scheduled_at: datetime
    status: FollowUpStatus
    body: str | None
    template_name: str | None
    template_language: str | None
    reason: str | None
    created_by_id: uuid.UUID | None
    created_by_kind: ActorKind
    attempts: int
    # Carries the skip reason as well as a failure, which is what tells a
    # workspace its nudge went unsent because the window had closed.
    last_error: str | None
    sent_at: datetime | None
    cancelled_at: datetime | None
    cancelled_reason: str | None
    message_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, follow_up: FollowUp) -> Self:
        return cls(
            id=follow_up.id,
            conversation_id=follow_up.conversation_id,
            lead_id=follow_up.lead_id,
            scheduled_at=follow_up.scheduled_at,
            status=follow_up.status,
            body=follow_up.body,
            template_name=follow_up.template_name,
            template_language=follow_up.template_language,
            reason=follow_up.reason,
            created_by_id=follow_up.created_by_id,
            created_by_kind=follow_up.created_by_kind,
            attempts=follow_up.attempts,
            last_error=follow_up.last_error,
            sent_at=follow_up.sent_at,
            cancelled_at=follow_up.cancelled_at,
            cancelled_reason=follow_up.cancelled_reason,
            message_id=follow_up.message_id,
            created_at=follow_up.created_at,
            updated_at=follow_up.updated_at,
        )


__all__ = [
    "MAX_DELAY_MINUTES",
    "MIN_DELAY_MINUTES",
    "FollowUpCancelRequest",
    "FollowUpCreateRequest",
    "FollowUpRead",
]
