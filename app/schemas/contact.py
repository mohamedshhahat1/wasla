"""Contact API contracts (FEB-19).

A contact's read, the one edit a person may make - the name the team gives the
customer - and the history of its marketing consent. Identities are read only:
they are what the channel reported, never typed (CRM-20, ADR-122).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.consent import ConsentAction
from app.db.models.conversation import MAX_CONTACT_NAME_LENGTH, shown_name
from app.schemas.campaign import ChannelConsentRead, ContactIdentityRead, consent_channels
from app.schemas.text import StorableText
from app.services.contact_service import ConsentEventView, ContactView

#: The `error_code` of a name that is empty once trimmed (send null to clear it).
CONTACT_NAME_INVALID = "contact_name_invalid"


class ContactRead(BaseModel):
    """A customer as a member reads them.

    `profile_name` is the name the channel gave - customer text, never markup -
    and `name` the one a member gave, or null. `display_name` is the name to
    show: `name`, else `profile_name`, else null; a conversation's
    `participant.display_name` follows the same rule.

    Consent is per channel (ENT-19): `channels` reads exactly as the opt-out
    answer does. `opted_out` is true when any channel is opted out and
    `opted_out_at` is the earliest standing refusal - a summary for a badge,
    never a person-wide switch.

    `created_at` is when this business first heard from them; `last_inbound_at`
    their newest message on any conversation, or null.
    """

    id: uuid.UUID
    identities: list[ContactIdentityRead]
    profile_name: str | None
    name: str | None
    display_name: str | None
    channels: list[ChannelConsentRead]
    opted_out: bool
    opted_out_at: datetime | None
    created_at: datetime
    last_inbound_at: datetime | None

    @classmethod
    def from_view(cls, view: ContactView) -> Self:
        contact = view.contact
        standing = [
            consent.marketing_opt_out_at
            for consent in view.consents
            if consent.marketing_opt_out_at is not None
        ]
        return cls(
            id=contact.id,
            identities=[ContactIdentityRead.from_model(identity) for identity in view.identities],
            profile_name=contact.display_name,
            name=contact.name,
            display_name=shown_name(contact.name, contact.display_name),
            channels=consent_channels(view.identities, view.consents),
            opted_out=bool(standing),
            opted_out_at=min(standing) if standing else None,
            created_at=contact.created_at,
            last_inbound_at=view.last_inbound_at,
        )


class ContactUpdateRequest(BaseModel):
    """The one edit: the name the team gives the customer.

    `name` is required: a string sets it, null takes it away. It is trimmed;
    one that is empty afterwards is refused (422 `contact_name_invalid`) - send
    null to clear. Any other key is refused: an identity is never edited.
    """

    model_config = ConfigDict(extra="forbid")

    name: StorableText | None = Field(max_length=MAX_CONTACT_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ConsentEventActor(BaseModel):
    """The member who recorded or cleared an opt-out; kept by name after they leave."""

    user_id: uuid.UUID
    display_name: str | None


class ConsentEventRead(BaseModel):
    """One change of a contact's marketing consent on one channel (CD-06).

    `source` is who decided (`customer`, `team`) and `via` by which evidence.
    `by` is the member who recorded it, null when the customer or the provider
    did. A row with `before_history: true` is an opt-out recorded before the
    history was kept: it has no `id`, `source`, `via` or `by`, and `at` is the
    refusal's own time.
    """

    id: uuid.UUID | None
    channel: Channel
    action: ConsentAction
    source: OptOutSource | None
    via: OptOutVia | None
    by: ConsentEventActor | None
    at: datetime
    before_history: bool

    @classmethod
    def from_view(cls, view: ConsentEventView) -> Self:
        return cls(
            id=view.id,
            channel=view.channel,
            action=view.action,
            source=view.source,
            via=view.via,
            by=(
                ConsentEventActor(user_id=view.actor_user_id, display_name=view.actor_name)
                if view.actor_user_id is not None
                else None
            ),
            at=view.at,
            before_history=view.before_history,
        )


__all__ = [
    "CONTACT_NAME_INVALID",
    "ConsentEventActor",
    "ConsentEventRead",
    "ContactRead",
    "ContactUpdateRequest",
]
