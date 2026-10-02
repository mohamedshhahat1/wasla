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
from collections.abc import Sequence
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
    OptOutVia,
    RecipientStatus,
)
from app.db.models.channel import Channel, ContactIdentity, IdentityKind
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact
from app.db.models.lead import LeadStatus
from app.repositories.campaign_repository import AudienceFilter, CampaignStatistics
from app.schemas.text import StorableText
from app.services.campaign_service import MAX_AUDIENCE_SIZE
from app.services.entitlement_terms import ordered

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
    name: StorableText = Field(min_length=1, max_length=MAX_CAMPAIGN_NAME_LENGTH)
    description: StorableText | None = Field(
        default=None, max_length=MAX_CAMPAIGN_DESCRIPTION_LENGTH
    )
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
    """Who decided, and on which channel (ENT-19): an opt-out is never person-wide."""

    channel: Channel
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


class ContactIdentityRead(BaseModel):
    """One way a channel addresses this person: a phone number, a business-scoped id."""

    id: uuid.UUID
    channel: Channel
    kind: IdentityKind
    value: str

    @classmethod
    def from_model(cls, identity: ContactIdentity) -> Self:
        return cls(
            id=identity.id, channel=identity.channel, kind=identity.kind, value=identity.value
        )


class ChannelConsentRead(BaseModel):
    """A person's marketing consent on one channel, and the identities it covers (ENT-19).

    A STOP on WhatsApp covers every WhatsApp number of the workspace and each
    way WhatsApp addresses the person - phone and business-scoped id alike -
    and nothing on another channel.
    """

    channel: Channel
    opted_out: bool
    marketing_opt_out_at: datetime | None
    opt_out_source: OptOutSource | None
    opt_out_via: OptOutVia | None
    resumed_at: datetime | None
    identities: list[ContactIdentityRead]


class ContactOptOutRead(BaseModel):
    """A person's marketing consent, channel by channel (ENT-19).

    `channels` has one entry for every channel the person has an identity or a
    recorded consent on, in vocabulary order; a channel with no entry has
    never been written on and never opted out. `identities` lists every
    identity, whatever its channel. `wa_id` is the WhatsApp phone number when
    there is one, and null for a customer WhatsApp knows only by username
    (OMNI-002).
    """

    id: uuid.UUID
    wa_id: str | None = Field(
        deprecated=(
            "Read the whatsapp `phone` entry of identities instead; null for a "
            "customer known only by a business-scoped id. Kept until clients have "
            "moved (docs/API.md)."
        ),
    )
    display_name: str | None
    channels: list[ChannelConsentRead]
    identities: list[ContactIdentityRead]

    @classmethod
    def from_model(
        cls,
        contact: Contact,
        *,
        identities: Sequence[ContactIdentity],
        consents: Sequence[ContactChannelConsent],
    ) -> Self:
        by_channel = {consent.channel: consent for consent in consents}
        present = {identity.channel for identity in identities} | set(by_channel)
        channels = []
        for channel in ordered(present):
            consent = by_channel.get(channel)
            channels.append(
                ChannelConsentRead(
                    channel=channel,
                    opted_out=consent is not None and not consent.accepts_marketing,
                    marketing_opt_out_at=consent.marketing_opt_out_at if consent else None,
                    opt_out_source=consent.opt_out_source if consent else None,
                    opt_out_via=consent.opt_out_via if consent else None,
                    resumed_at=consent.resumed_at if consent else None,
                    identities=[
                        ContactIdentityRead.from_model(identity)
                        for identity in identities
                        if identity.channel is channel
                    ],
                )
            )
        return cls(
            id=contact.id,
            wa_id=contact.wa_id,
            display_name=contact.display_name,
            channels=channels,
            identities=[ContactIdentityRead.from_model(identity) for identity in identities],
        )


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
    "ChannelConsentRead",
    "ContactIdentityRead",
    "ContactOptOutRead",
    "OptOutRequest",
    "RecipientListResponse",
    "RecipientRead",
]
