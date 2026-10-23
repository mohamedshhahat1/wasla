"""Follow-up API contracts.

The create request accepts either a delay or an absolute time, and exactly one
of them. Both are genuinely useful — "in half an hour" is how a person thinks
during a conversation, "on Tuesday at nine" is how they think when planning —
and accepting both at once would leave the service picking a winner silently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.timezones import local
from app.db.models.follow_up import (
    MAX_BODY_LENGTH,
    MAX_REASON_LENGTH,
    FollowUp,
    FollowUpDeferReason,
    FollowUpStatus,
)
from app.db.models.lead import ActorKind
from app.schemas.bounds import TEMPLATE_COMPONENTS, check_json
from app.schemas.text import StorableText
from app.services.follow_up_sequences import FollowUpState
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
    # Must carry its UTC offset (PD-CRM-9). A naive time used to be read as
    # UTC, so a colleague in Cairo typing 09:00 got the nudge at noon or 11:00
    # depending on the season (CRM-15). Stored normalised to UTC.
    scheduled_at: AwareDatetime | None = None

    body: StorableText | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    template_name: StorableText | None = Field(default=None, max_length=512)
    template_language: StorableText | None = Field(default=None, min_length=2, max_length=16)
    # Forwarded to Meta when the follow-up fires, so it is bounded on the way
    # in rather than on the way out - see `app.schemas.bounds`.
    template_components: list[dict[str, Any]] | None = None
    # The template header's file, from the workspace's library (HMT-03).
    header_media_id: uuid.UUID | None = None

    @field_validator("template_components")
    @classmethod
    def _bounded_components(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None:
            check_json(value, TEMPLATE_COMPONENTS, field="template_components")
        return value

    reason: StorableText | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    # This workspace's lead of this conversation's customer, or 404/422.
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

    reason: StorableText | None = Field(default=None, max_length=MAX_REASON_LENGTH)


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
    # The time asked for, never moved by a wait (FQH-04); `scheduled_at` is
    # when it is due now. `deferred_reason` says why the two differ:
    # `portfolio_limit` (Meta's limit at 100 %, FLIM-01) or `quiet_hours` (a
    # template follow-up waiting for the morning, FQH-01); null otherwise.
    requested_at: datetime | None = None
    deferred_reason: FollowUpDeferReason | None = None
    # Both in the workspace's own zone, with its offset (TZ-05).
    due_at_local: datetime | None = None
    requested_at_local: datetime | None = None

    @classmethod
    def from_model(cls, follow_up: FollowUp, *, timezone: str | None = None) -> Self:
        read = cls._read(follow_up)
        read.requested_at = follow_up.requested_at
        read.deferred_reason = (
            FollowUpDeferReason(follow_up.deferred_reason) if follow_up.deferred_reason else None
        )
        if timezone is not None:
            read.due_at_local = local(follow_up.scheduled_at, timezone)
            if follow_up.requested_at is not None:
                read.requested_at_local = local(follow_up.requested_at, timezone)
        return read

    @classmethod
    def _read(cls, follow_up: FollowUp) -> Self:
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


class FollowUpStateRead(BaseModel):
    """A conversation's follow-ups as the inbox shows them (FUS-09).

    `scheduled`: one waits, due `due_at` (a reminder of a run has `step_index`
    of `steps_total`; a person's one-off has neither). `ended`: the last run
    ended, why and when. `none`: nothing was ever scheduled, or a one-off went.
    """

    state: Literal["none", "scheduled", "ended"]
    step_index: int | None
    steps_total: int | None
    due_at: datetime | None
    due_at_local: datetime | None
    ended_reason: str | None
    ended_at: datetime | None

    @classmethod
    def from_state(cls, state: FollowUpState, *, timezone: str) -> Self:
        return cls(
            state=state.state,
            step_index=state.step_index,
            steps_total=state.steps_total,
            due_at=state.due_at,
            due_at_local=local(state.due_at, timezone) if state.due_at else None,
            ended_reason=state.ended_reason,
            ended_at=state.ended_at,
        )


class FollowUpHistoryRead(BaseModel):
    """One follow-up of a conversation, as its history lists it (FUS-09).

    `text` is what was sent - only for a sent one; a skipped step never shows
    what the model wrote. `skip_reason` and `cancelled_reason` are closed codes.
    """

    id: uuid.UUID
    origin: str
    step_index: int | None
    status: FollowUpStatus
    requested_at: datetime | None
    scheduled_at: datetime
    scheduled_at_local: datetime
    sent_at: datetime | None
    cancelled_at: datetime | None
    cancelled_reason: str | None
    skip_reason: str | None
    deferred_reason: str | None
    text: str | None
    message_id: uuid.UUID | None
    created_by_kind: ActorKind

    @classmethod
    def from_model(cls, follow_up: FollowUp, *, timezone: str) -> Self:
        sent = follow_up.status is FollowUpStatus.SENT
        return cls(
            id=follow_up.id,
            origin=follow_up.origin,
            step_index=follow_up.step_index,
            status=follow_up.status,
            requested_at=follow_up.requested_at,
            scheduled_at=follow_up.scheduled_at,
            scheduled_at_local=local(follow_up.scheduled_at, timezone),
            sent_at=follow_up.sent_at,
            cancelled_at=follow_up.cancelled_at,
            cancelled_reason=follow_up.cancelled_reason,
            skip_reason=follow_up.skip_reason,
            deferred_reason=follow_up.deferred_reason,
            text=(follow_up.body if sent else None),
            message_id=follow_up.message_id,
            created_by_kind=follow_up.created_by_kind,
        )


__all__ = [
    "MAX_DELAY_MINUTES",
    "MIN_DELAY_MINUTES",
    "FollowUpCancelRequest",
    "FollowUpCreateRequest",
    "FollowUpHistoryRead",
    "FollowUpRead",
    "FollowUpStateRead",
]
