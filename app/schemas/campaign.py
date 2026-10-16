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
from datetime import date, datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.core.timezones import local
from app.db.models.campaign import (
    DEFAULT_MESSAGES_PER_MINUTE,
    MAX_CAMPAIGN_NAME_LENGTH,
    MAX_MESSAGES_PER_MINUTE,
    MIN_MESSAGES_PER_MINUTE,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    CampaignStopReason,
    CampaignWaitReason,
    OptOutSource,
    OptOutVia,
    RecipientReason,
    RecipientStatus,
)
from app.db.models.campaign_media import CampaignHeaderMedia, HeaderMediaKind
from app.db.models.channel import Channel, ContactIdentity, IdentityKind
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact
from app.db.models.lead import LeadStatus
from app.repositories.campaign_repository import AudienceFilter, CampaignStatistics
from app.schemas.text import StorableText
from app.services.campaign_planner import Eta
from app.services.campaign_service import MAX_AUDIENCE_SIZE
from app.services.delivery_plan import DeliveryPlan, EtaBasis, PlanNotice
from app.services.entitlement_terms import ordered

# How many variables a template can plausibly want. Meta's own limit is higher;
# this is a bound on a request body, not a statement about templates.
MAX_TEMPLATE_VARIABLES = 20
MAX_VARIABLE_LENGTH = 1024
MAX_TEMPLATE_BUTTONS = 10
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


class ButtonParameterRequest(_Payload):
    """A dynamic button's send-time value: a URL suffix, or a code to copy (CMP-05)."""

    # Meta allows up to ten buttons on a template.
    index: int = Field(ge=0, le=MAX_TEMPLATE_BUTTONS - 1)
    value: str = Field(min_length=1, max_length=MAX_VARIABLE_LENGTH)


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
    # A text header's variables, in placeholder order (CMP-05). Validated
    # against the template's stored components in the service; a media header
    # is refused there.
    header_variables: list[Annotated[str, Field(max_length=MAX_VARIABLE_LENGTH)]] = Field(
        default_factory=list,
        max_length=MAX_TEMPLATE_VARIABLES,
    )
    # One value per dynamic button - a URL button whose URL ends in a
    # placeholder, or a copy-code button (CMP-05).
    button_parameters: list[ButtonParameterRequest] = Field(
        default_factory=list,
        max_length=MAX_TEMPLATE_BUTTONS,
    )
    # The file a template with an IMAGE, VIDEO or DOCUMENT header is sent with:
    # an id from `POST /campaigns/header-media` (HM-02). Required by such a
    # template, refused by any other; fixed once the campaign is composed.
    header_media_id: uuid.UUID | None = None


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
    # When to stop trying (LIM-06): whoever is still pending then is skipped
    # `send_window_closed` and the campaign completes. With its UTC offset,
    # after the start.
    send_until: datetime | None = None


class PlanDayRead(BaseModel):
    """One local day of a delivery plan (LIM-05)."""

    local_date: date
    first_send_at: datetime
    first_send_at_local: datetime
    last_send_at: datetime
    last_send_at_local: datetime
    recipients: int


class DeliveryPlanRead(BaseModel):
    """When the campaign is expected to reach its people, day by day (LIM-05).

    A prediction from the campaign's rate, the workspace's quiet hours, the
    portfolio's campaign budget and what it has already used, and per-person
    deferrals - never a guarantee. `notices` says what shaped it.
    """

    computed_at: datetime
    timezone: str
    first_send_at: datetime | None
    first_send_at_local: datetime | None
    estimated_completion_at: datetime | None
    estimated_completion_at_local: datetime | None
    recipients_planned: int
    recipients_left_by_deadline: int
    days: list[PlanDayRead]
    notices: list[PlanNotice]
    basis: list[EtaBasis]

    @classmethod
    def from_plan(cls, planned: DeliveryPlan) -> Self:
        tz = planned.timezone

        def at(moment: datetime | None) -> datetime | None:
            return local(moment, tz) if moment is not None else None

        return cls(
            computed_at=planned.computed_at,
            timezone=tz,
            first_send_at=planned.first_send_at,
            first_send_at_local=at(planned.first_send_at),
            estimated_completion_at=planned.estimated_completion_at,
            estimated_completion_at_local=at(planned.estimated_completion_at),
            recipients_planned=planned.planned,
            recipients_left_by_deadline=planned.left_by_deadline,
            days=[
                PlanDayRead(
                    local_date=day.local_date,
                    first_send_at=day.first_send_at,
                    first_send_at_local=local(day.first_send_at, tz),
                    last_send_at=day.last_send_at,
                    last_send_at_local=local(day.last_send_at, tz),
                    recipients=day.recipients,
                )
                for day in planned.days
            ],
            notices=list(planned.notices),
            basis=list(planned.basis),
        )


class EtaRead(BaseModel):
    """When a live campaign is expected to finish (PG-13) - an estimate, never a promise.

    Null `estimated_completion_at` with a `reason` for a campaign that will not
    send by itself (draft, paused, failed, cancelled, completed) or whose
    number's limit is unknown.
    """

    estimated_completion_at: datetime | None
    estimated_completion_at_local: datetime | None
    basis: list[EtaBasis]
    confidence: str | None
    reason: str | None

    @classmethod
    def from_eta(cls, eta: Eta, *, timezone: str) -> Self:
        return cls(
            estimated_completion_at=eta.estimated_completion_at,
            estimated_completion_at_local=(
                local(eta.estimated_completion_at, timezone)
                if eta.estimated_completion_at is not None
                else None
            ),
            basis=list(eta.basis),
            confidence=eta.confidence,
            reason=eta.reason,
        )


class OptOutRequest(_Payload):
    """Who decided, and on which channel (ENT-19): an opt-out is never person-wide."""

    channel: Channel
    source: OptOutSource = OptOutSource.TEAM


class HeaderMediaRead(BaseModel):
    """A campaign header file as the API shows it (HM-02, HM-08).

    What a colleague needs to recognise the file and nothing else: no storage
    key, no link, and never Meta's id for it.
    """

    id: uuid.UUID
    kind: HeaderMediaKind
    mime_type: str
    size_bytes: int
    # A document's name; null for an image or a video.
    filename: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, asset: CampaignHeaderMedia) -> Self:
        return cls(
            id=asset.id,
            kind=HeaderMediaKind(asset.kind),
            mime_type=asset.mime_type,
            size_bytes=asset.byte_size,
            filename=asset.filename,
            created_at=asset.created_at,
        )


class HeaderMediaUploadRead(HeaderMediaRead):
    """The answer to an upload: the file as stored, with its SHA-256."""

    sha256: str

    @classmethod
    def from_model(cls, asset: CampaignHeaderMedia) -> Self:
        return cls(
            id=asset.id,
            kind=HeaderMediaKind(asset.kind),
            mime_type=asset.mime_type,
            size_bytes=asset.byte_size,
            filename=asset.filename,
            created_at=asset.created_at,
            sha256=asset.content_hash,
        )


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    template_id: uuid.UUID
    name: str
    description: str | None
    status: CampaignStatus
    variables: list[str] | None
    # The header's and the buttons' send-time parameters (CMP-05), when the
    # template needs any: `{"header": [...], "buttons": [{"index", "value"}]}`.
    template_parameters: dict[str, Any] | None = None
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
    # Why it is paused or failed, from a closed vocabulary (CMP-02, CMP-23):
    # `workspace_suspended`, `subscription_suspended`, `admin_paused`,
    # `template_withdrawn`, `credential_refused` and the rest of
    # `CampaignStopReason`. Null while it is sending, or done.
    status_reason: CampaignStopReason | None = None
    # The header file it is sent with (HM-08), or null for a template whose
    # header is text or absent. Meta's id for it is never shown.
    header_media: HeaderMediaRead | None = None
    # Why a live campaign is sending nothing right now and will by itself
    # (TZ-03, LIM-04, FREQ-01): `quiet_hours`, `daily_limit`, `contact_cap`,
    # `limit_unknown`. Not a pause - the status stays scheduled or running.
    # Null while it sends, and in every other status.
    waiting_reason: CampaignWaitReason | None = None
    # When it will send again, in UTC and in the workspace's own time with
    # its offset (TZ-03). Null unless it is waiting.
    resumes_at: datetime | None = None
    resumes_at_local: datetime | None = None
    # Its deadline (LIM-06), in UTC and in the workspace's own time.
    send_until: datetime | None = None
    send_until_local: datetime | None = None
    # When a live campaign is expected to finish (PG-13). Computed for one
    # campaign's read, a list of live ones and the progress route.
    eta: EtaRead | None = None
    # The plan, on the schedule response only (LIM-05); `GET
    # /campaigns/{id}/delivery-plan` recomputes it at any time.
    delivery_plan: DeliveryPlanRead | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        campaign: Campaign,
        *,
        header_media: CampaignHeaderMedia | None = None,
        timezone: str | None = None,
        eta: Eta | None = None,
    ) -> Self:
        read = cls.model_validate(campaign)
        if header_media is not None:
            read.header_media = HeaderMediaRead.from_model(header_media)
        if timezone is not None and read.resumes_at is not None:
            read.resumes_at_local = local(read.resumes_at, timezone)
        if timezone is not None and read.send_until is not None:
            read.send_until_local = local(read.send_until, timezone)
        if timezone is not None and eta is not None:
            read.eta = EtaRead.from_eta(eta, timezone=timezone)
        return read


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
    # The same reason as a closed code a program can filter on (CMP-13):
    # `opted_out`, `marketing_limit_per_user`, `delivery_unconfirmed`, ...
    # Null when sent, and on rows recorded before reason codes.
    reason_code: RecipientReason | None = None
    # Exactly who this copy is addressed to (OMNI-004).
    participant_identity_id: uuid.UUID | None = None
    sent_at: datetime | None

    @classmethod
    def from_model(cls, recipient: CampaignRecipient) -> Self:
        return cls.model_validate(recipient)


class RecipientListResponse(BaseModel):
    recipients: list[RecipientRead]
    # Pass back as `cursor` for the next page; null on the last (CMP-13).
    next_cursor: str | None = None


class CampaignStatisticsRead(BaseModel):
    """Outcomes, plus what Meta has since said about delivery."""

    pending: int
    sent: int
    failed: int
    skipped: int
    delivered: int
    read: int
    total: int
    # Sent - Meta accepted them - then reported `failed` (CMP-10).
    failed_after_send: int = 0
    # Accepted, and held by Meta's portfolio pacing while it gathers feedback
    # (LIM-08). Counted among `sent`.
    held: int = 0

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
            failed_after_send=statistics.failed_after_send,
            held=statistics.held,
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
    "ButtonParameterRequest",
    "CampaignCreateRequest",
    "CampaignRead",
    "CampaignScheduleRequest",
    "CampaignStatisticsRead",
    "ChannelConsentRead",
    "ContactIdentityRead",
    "ContactOptOutRead",
    "DeliveryPlanRead",
    "EtaRead",
    "OptOutRequest",
    "PlanDayRead",
    "RecipientListResponse",
    "RecipientRead",
]
