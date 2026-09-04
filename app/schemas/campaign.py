"""Campaign API contracts.

Two absences are the contract as much as anything present.

There is no `body` on a create request. A campaign sends an approved template
and nothing else, so a free-text field would be a promise the platform cannot
keep once the request reaches Meta.

There is no way to name a recipient who is not already a contact. The audience
request carries filters, never phone numbers, so the only people a campaign can
reach are the ones who chose to write to this business.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.campaign import (
    DEFAULT_MESSAGES_PER_MINUTE,
    MAX_CAMPAIGN_NAME_LENGTH,
    MAX_MESSAGES_PER_MINUTE,
    MIN_MESSAGES_PER_MINUTE,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    OptOutSource,
    RecipientStatus,
)
from app.db.models.lead import LeadStatus
from app.repositories.campaign_repository import AudienceFilter, CampaignStatistics
from app.services.campaign_service import MAX_AUDIENCE_SIZE

# How many variables a template can plausibly want. Meta's own limit is higher;
# this is a bound on a request body, not a statement about templates.
MAX_TEMPLATE_VARIABLES = 20
MAX_VARIABLE_LENGTH = 1024
# A campaign description is a note to whoever runs the campaign next, and the
# column is `Text` - so nothing but this decides how long it may be. Two
# thousand characters is several paragraphs, which is more than anybody writes
# about a broadcast and far short of a document.
MAX_CAMPAIGN_DESCRIPTION_LENGTH = 2000
# Enough for a full year of "customers who wrote to us recently", and short
# enough that a stray number cannot mean "everyone who ever wrote to us".
MAX_RECENCY_DAYS = 365


class _Payload(BaseModel):
    """Request bodies reject unknown fields rather than ignoring them."""

    model_config = ConfigDict(extra="forbid")


class CampaignCreateRequest(_Payload):
    """A draft. Nothing is sent and no audience exists until both are asked for."""

    account_id: uuid.UUID
    template_id: uuid.UUID
    name: str = Field(min_length=1, max_length=MAX_CAMPAIGN_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_CAMPAIGN_DESCRIPTION_LENGTH)
    # In the order the template's placeholders appear. Validated against the
    # template's own variable count in the service, which is the only place that
    # knows what the template says.
    variables: list[Annotated[str, Field(max_length=MAX_VARIABLE_LENGTH)]] = Field(
        default_factory=list,
        max_length=MAX_TEMPLATE_VARIABLES,
    )
    messages_per_minute: int = Field(
        default=DEFAULT_MESSAGES_PER_MINUTE,
        ge=MIN_MESSAGES_PER_MINUTE,
        le=MAX_MESSAGES_PER_MINUTE,
    )


class AudienceRequest(_Payload):
    """Filters that narrow the people a campaign may reach. None of them widens it."""

    last_inbound_within_days: int | None = Field(default=None, ge=1, le=MAX_RECENCY_DAYS)
    lead_statuses: list[LeadStatus] = Field(default_factory=list)
    # Contacts of this workspace, chosen by hand. Still filtered by everything
    # else: naming somebody who opted out does not reach them.
    contact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)

    def to_filter(self) -> AudienceFilter:
        return AudienceFilter(
            last_inbound_within_days=self.last_inbound_within_days,
            lead_statuses=tuple(self.lead_statuses),
            contact_ids=tuple(self.contact_ids),
        )


class AudiencePreviewRequest(AudienceRequest):
    """The same filters, asked about a number rather than an existing campaign."""

    account_id: uuid.UUID


class AudiencePreviewResponse(BaseModel):
    account_id: uuid.UUID
    size: int
    limit: int = MAX_AUDIENCE_SIZE


class CampaignScheduleRequest(_Payload):
    """When to send. Omitting the time means now."""

    scheduled_at: datetime | None = None


class OptOutRequest(_Payload):
    source: OptOutSource = OptOutSource.TEAM


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    template_id: uuid.UUID
    name: str
    description: str | None
    status: CampaignStatus
    variables: list[str] | None
    audience: dict[str, Any] | None
    audience_size: int
    messages_per_minute: int
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    next_send_at: datetime | None
    created_by_id: uuid.UUID | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, campaign: Campaign) -> Self:
        return cls.model_validate(campaign)


class RecipientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    campaign_id: uuid.UUID
    contact_id: uuid.UUID
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    status: RecipientStatus
    attempts: int
    # Carries the skip reason as well as a failure, which is what tells a
    # workspace somebody was left out because they had opted out.
    last_error: str | None
    sent_at: datetime | None

    @classmethod
    def from_model(cls, recipient: CampaignRecipient) -> Self:
        return cls.model_validate(recipient)


class RecipientListResponse(BaseModel):
    recipients: list[RecipientRead]


class CampaignStatisticsRead(BaseModel):
    """Outcomes, plus what Meta has since said about delivery."""

    pending: int
    sent: int
    failed: int
    skipped: int
    delivered: int
    read: int
    total: int

    @classmethod
    def from_statistics(cls, statistics: CampaignStatistics) -> Self:
        return cls(
            pending=statistics.pending,
            sent=statistics.sent,
            failed=statistics.failed,
            skipped=statistics.skipped,
            delivered=statistics.delivered,
            read=statistics.read,
            total=statistics.total,
        )


class ContactOptOutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    wa_id: str
    display_name: str | None
    marketing_opt_out_at: datetime | None
    opt_out_source: OptOutSource | None


__all__ = [
    "MAX_RECENCY_DAYS",
    "MAX_TEMPLATE_VARIABLES",
    "MAX_VARIABLE_LENGTH",
    "AudiencePreviewRequest",
    "AudiencePreviewResponse",
    "AudienceRequest",
    "CampaignCreateRequest",
    "CampaignRead",
    "CampaignScheduleRequest",
    "CampaignStatisticsRead",
    "ContactOptOutRead",
    "OptOutRequest",
    "RecipientListResponse",
    "RecipientRead",
]
