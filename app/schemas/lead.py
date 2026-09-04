"""Lead API contracts.

Read models map field by field rather than inferring from the ORM object, so
adding a column to `leads` never silently widens the API.

The update request is the one unusual shape here. Every field defaults to a
sentinel meaning "not supplied", because null has to stay available as a real
value: clearing a budget someone mistyped is a legitimate edit, and a schema
that cannot express it forces callers into workarounds.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.lead import (
    MAX_SCORE,
    MIN_SCORE,
    ActorKind,
    Lead,
    LeadActivity,
    LeadActivityKind,
    LeadNote,
    LeadSource,
    LeadStatus,
)
from app.repositories.lead_repository import LeadStatistics
from app.schemas.bounds import LEAD_CUSTOM_FIELDS, check_json
from app.services.lead_service import (
    MAX_INTEREST_LENGTH,
    MAX_NOTE_LENGTH,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    UNSET,
    LeadUpdate,
)


class LeadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    interest: str | None = Field(default=None, max_length=MAX_INTEREST_LENGTH)
    # `max_digits` matches `leads.budget_amount`, which is `Numeric(14, 2)`.
    # Without it a caller could post a million-digit number, which pydantic
    # parses before PostgreSQL rejects it.
    budget_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3)
    source: LeadSource = LeadSource.MANUAL
    assigned_to_id: uuid.UUID | None = None
    # The per-tag bound restates what the service already enforces, so a caller
    # gets a 422 naming the field rather than a 400 naming the rule.
    tags: list[Annotated[str, Field(max_length=MAX_TAG_LENGTH)]] | None = Field(
        default=None, max_length=MAX_TAGS
    )
    custom_fields: dict[str, Any] | None = None

    @field_validator("custom_fields")
    @classmethod
    def _bounded_custom_fields(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            check_json(value, LEAD_CUSTOM_FIELDS, field="custom_fields")
        return value


class LeadUpdateRequest(BaseModel):
    """A partial edit.

    Fields omitted entirely are left alone; fields sent as null are cleared.
    Pydantic cannot express that with `None` alone, so omission is detected
    through `model_fields_set` and converted to the service's sentinel.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    interest: str | None = Field(default=None, max_length=MAX_INTEREST_LENGTH)
    budget_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3)
    tags: list[Annotated[str, Field(max_length=MAX_TAG_LENGTH)]] | None = Field(
        default=None, max_length=MAX_TAGS
    )
    custom_fields: dict[str, Any] | None = None

    @field_validator("custom_fields")
    @classmethod
    def _bounded_custom_fields(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            check_json(value, LEAD_CUSTOM_FIELDS, field="custom_fields")
        return value

    def to_update(self) -> LeadUpdate:
        """Convert omitted fields to the sentinel the service expects."""
        supplied = self.model_fields_set

        def value(name: str) -> Any:
            return getattr(self, name) if name in supplied else UNSET

        return LeadUpdate(
            name=value("name"),
            phone=value("phone"),
            email=value("email"),
            interest=value("interest"),
            budget_amount=value("budget_amount"),
            budget_currency=value("budget_currency"),
            # Not sentinel-guarded: a null here means "no change" rather than
            # "clear", because an empty list already expresses clearing them.
            tags=self.tags,
            custom_fields=self.custom_fields,
        )


class LeadStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LeadStatus
    reason: str | None = Field(default=None, max_length=300)


class LeadAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null clears the assignment. A value must belong to this workspace.
    assigned_to_id: uuid.UUID | None = None


class LeadScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=MIN_SCORE, le=MAX_SCORE)


class LeadNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)


class TagList(BaseModel):
    """Shared constraint for tag input."""

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(max_length=MAX_TAGS)


class LeadRead(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    name: str | None
    phone: str | None
    email: str | None
    interest: str | None
    budget_amount: Decimal | None
    budget_currency: str | None
    status: LeadStatus
    source: LeadSource
    score: int
    assigned_to_id: uuid.UUID | None
    tags: list[str]
    custom_fields: dict[str, Any]
    # Exposed so an interface can show which fields an agent will not touch.
    human_verified_fields: list[str]
    qualified_at: datetime | None
    closed_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, lead: Lead) -> Self:
        return cls(
            id=lead.id,
            contact_id=lead.contact_id,
            conversation_id=lead.conversation_id,
            name=lead.name,
            phone=lead.phone,
            email=lead.email,
            interest=lead.interest,
            budget_amount=lead.budget_amount,
            budget_currency=lead.budget_currency,
            status=lead.status,
            source=lead.source,
            score=lead.score,
            assigned_to_id=lead.assigned_to_id,
            tags=list(lead.tags),
            custom_fields=dict(lead.custom_fields),
            human_verified_fields=list(lead.human_verified_fields),
            qualified_at=lead.qualified_at,
            closed_at=lead.closed_at,
            last_activity_at=lead.last_activity_at,
            created_at=lead.created_at,
            updated_at=lead.updated_at,
        )


class LeadNoteRead(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    author_id: uuid.UUID | None
    author_kind: ActorKind
    body: str
    created_at: datetime

    @classmethod
    def from_model(cls, note: LeadNote) -> Self:
        return cls(
            id=note.id,
            lead_id=note.lead_id,
            author_id=note.author_id,
            author_kind=note.author_kind,
            body=note.body,
            created_at=note.created_at,
        )


class LeadActivityRead(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    kind: LeadActivityKind
    actor_id: uuid.UUID | None
    actor_kind: ActorKind
    summary: str
    data: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def from_model(cls, activity: LeadActivity) -> Self:
        return cls(
            id=activity.id,
            lead_id=activity.lead_id,
            kind=activity.kind,
            actor_id=activity.actor_id,
            actor_kind=activity.actor_kind,
            summary=activity.summary,
            data=activity.data,
            created_at=activity.created_at,
        )


class LeadStatisticsRead(BaseModel):
    """Pipeline counts for a workspace.

    `by_status` names every status, including the ones at zero, so a dashboard
    does not have to know the vocabulary to render an empty column.
    """

    total: int
    open_leads: int
    unassigned: int
    by_status: dict[LeadStatus, int]

    @classmethod
    def from_statistics(cls, statistics: LeadStatistics) -> Self:
        return cls(
            total=statistics.total,
            open_leads=statistics.open_leads,
            unassigned=statistics.unassigned,
            by_status={status: statistics.by_status.get(status, 0) for status in LeadStatus},
        )


__all__ = [
    "MAX_TAG_LENGTH",
    "LeadActivityRead",
    "LeadAssignmentRequest",
    "LeadCreateRequest",
    "LeadNoteRead",
    "LeadNoteRequest",
    "LeadRead",
    "LeadScoreRequest",
    "LeadStatisticsRead",
    "LeadStatusRequest",
    "LeadUpdateRequest",
    "TagList",
]
