"""Saved campaign segments, as the API takes and shows them (PG-06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.campaign_segment import (
    MAX_SEGMENT_DESCRIPTION_LENGTH,
    MAX_SEGMENT_NAME_LENGTH,
    CampaignSegment,
)
from app.db.models.lead import LeadStatus
from app.repositories.campaign_repository import AudienceFilter
from app.schemas.campaign import MAX_RECENCY_DAYS
from app.schemas.text import StorableText


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SegmentRules(_Payload):
    """The rules a segment holds: never contact ids (an unknown field is refused)."""

    last_inbound_within_days: int | None = Field(default=None, ge=1, le=MAX_RECENCY_DAYS)
    lead_statuses: list[LeadStatus] = Field(default_factory=list, max_length=len(LeadStatus))

    def to_filter(self) -> AudienceFilter:
        return AudienceFilter(
            last_inbound_within_days=self.last_inbound_within_days,
            lead_statuses=tuple(dict.fromkeys(self.lead_statuses)),
        )


class SegmentCreateRequest(_Payload):
    name: StorableText = Field(min_length=1, max_length=MAX_SEGMENT_NAME_LENGTH)
    description: StorableText | None = Field(
        default=None, max_length=MAX_SEGMENT_DESCRIPTION_LENGTH
    )
    rules: SegmentRules
    # A number of this workspace the segment is meant for; null for any.
    account_id: uuid.UUID | None = None


class SegmentUpdateRequest(_Payload):
    """Only the fields sent change; `version` unless it comes as If-Match."""

    name: StorableText | None = Field(
        default=None, min_length=1, max_length=MAX_SEGMENT_NAME_LENGTH
    )
    description: StorableText | None = Field(
        default=None, max_length=MAX_SEGMENT_DESCRIPTION_LENGTH
    )
    rules: SegmentRules | None = None
    account_id: uuid.UUID | None = None
    version: int | None = Field(default=None, ge=1)

    def changes(self) -> dict[str, Any]:
        sent = self.model_fields_set - {"version"}
        changes: dict[str, Any] = {}
        for field in sent:
            value = getattr(self, field)
            if field == "rules":
                if value is None:
                    continue
                changes[field] = value.to_filter()
            elif field == "name" and value is None:
                continue
            else:
                changes[field] = value
        return changes


class SegmentRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    rules: dict[str, Any]
    account_id: uuid.UUID | None
    created_by_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, segment: CampaignSegment) -> Self:
        return cls(
            id=segment.id,
            name=segment.name,
            description=segment.description,
            rules=dict(segment.rules),
            account_id=segment.account_id,
            created_by_id=segment.created_by_id,
            version=segment.version,
            created_at=segment.created_at,
            updated_at=segment.updated_at,
        )


class SegmentListResponse(BaseModel):
    items: list[SegmentRead]


__all__ = [
    "SegmentCreateRequest",
    "SegmentListResponse",
    "SegmentRead",
    "SegmentRules",
    "SegmentUpdateRequest",
]
