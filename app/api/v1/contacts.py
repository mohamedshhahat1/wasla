"""Contact endpoints.

A contact is created by the webhook from what the channel reports, and its
identities are never typed or edited (CRM-20, ADR-122), so this is not a CRUD
resource and deliberately does not read like one. What a member can do:

- **find** contacts - the listing, which is also the campaigns page's contact
  picker (PG-07): search by name or number, and - on a chosen number - who a
  campaign could reach;
- **read** one (FEB-19): its identities, the channel's profile name, the name
  the team gave, its consent channel by channel;
- **name** it: the one edit, kept apart from the profile name and shown before
  it wherever the customer is named;
- **read its consent history**: every opt-out and every clear, newest first.

An opt-out is per channel (ENT-19): a STOP on WhatsApp covers every WhatsApp
number of the workspace and nothing on Instagram or Messenger, so both
operations name the channel, and the answer reads the consent channel by
channel.

The two operations have different weights, and the roles say so. **Recording**
an opt-out is any member's to do: the person handling the conversation is the
one a customer says "stop sending me these" to, and making them find an
administrator first is how the request gets lost. **Clearing** one takes an
administrator, because undoing somebody's own refusal is a decision a workspace
should have to make deliberately. Both are remembered in the history, with the
member who did it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.dependencies import (
    ActiveWorkspaceDep,
    CampaignServiceDep,
    ContactServiceDep,
    TenantAdminDep,
)
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep
from app.core.exceptions import ValidationError
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.channel import Channel
from app.db.models.lead import LeadStatus
from app.repositories.contact_picker_repository import PickedContact
from app.schemas.campaign import ContactOptOutRead, OptOutRequest
from app.schemas.contact import (
    CONTACT_NAME_INVALID,
    ConsentEventRead,
    ContactRead,
    ContactUpdateRequest,
)
from app.schemas.conversation import CursorPage
from app.services.contact_picker import ContactPicker

router = APIRouter(route_class=CommittingRoute, prefix="/contacts", tags=["contacts"])

#: The picker's largest page and longest search (PG-07).
MAX_PICKER_PAGE = 50
MAX_PICKER_SEARCH = 100


class PickedContactLead(BaseModel):
    id: uuid.UUID
    name: str | None
    status: LeadStatus


class PickedContactRead(BaseModel):
    """One contact as the picker shows it."""

    id: uuid.UUID
    # The name to show: the one a member gave, else the channel's (CD-05).
    display_name: str | None
    # The name a member gave, or null (FEB-19).
    name: str | None
    wa_id: str | None
    lead: PickedContactLead | None
    # On a number: that conversation's last inbound message. Without one: when
    # the contact was last seen anywhere.
    last_inbound_at: datetime | None
    # A refusal of marketing on the number's channel (WhatsApp without a number).
    opted_out_at: datetime | None
    # Whether a campaign from the number could reach them; null without a number.
    eligible_for_campaign: bool | None
    # `opted_out`, or null.
    ineligible_reason: str | None

    @classmethod
    def from_row(cls, row: PickedContact) -> PickedContactRead:
        return cls(
            id=row.id,
            display_name=row.display_name,
            name=row.name,
            wa_id=row.wa_id,
            lead=(
                PickedContactLead(id=row.lead_id, name=row.lead_name, status=row.lead_status)
                if row.lead_id is not None and row.lead_status is not None
                else None
            ),
            last_inbound_at=row.last_inbound_at,
            opted_out_at=row.opted_out_at,
            eligible_for_campaign=row.eligible_for_campaign,
            ineligible_reason=row.ineligible_reason,
        )


@router.get("", response_model=CursorPage[PickedContactRead], summary="Find contacts")
async def list_contacts(
    workspace: ActiveWorkspaceDep,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=MAX_PICKER_SEARCH)] = None,
    account_id: uuid.UUID | None = None,
    eligible_for_campaign: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PICKER_PAGE)] = MAX_PICKER_PAGE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> CursorPage[PickedContactRead]:
    """The campaigns page's contact picker (PG-07), newest activity first.

    `q`: digits search the WhatsApp number, anything else the contact's name -
    the one a member gave or the channel's - or their active lead's, with
    Turkish letters folded (the dotted and dotless i are one letter).
    `account_id`: contacts with a conversation on that number, each with
    whether a campaign from it could reach them;
    `eligible_for_campaign=true` keeps only those (it needs `account_id`, 422
    `account_required` otherwise). Any member.
    """
    page = await ContactPicker(session, tenant_id=workspace.tenant.id).page(
        q=q,
        account_id=account_id,
        eligible_for_campaign=eligible_for_campaign,
        limit=limit,
        cursor=cursor,
    )
    return CursorPage[PickedContactRead](
        items=[PickedContactRead.from_row(row) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{contact_id}", response_model=ContactRead, summary="Read a contact")
async def get_contact(contact_id: uuid.UUID, contacts: ContactServiceDep) -> ContactRead:
    """Who a customer is (FEB-19): identities, names, consent channel by channel.

    Any member. Another workspace's contact is the same 404 as an unknown id.
    """
    return ContactRead.from_view(await contacts.read(contact_id))


@router.patch("/{contact_id}", response_model=ContactRead, summary="Name a contact")
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdateRequest,
    workspace: ActiveWorkspaceDep,
    contacts: ContactServiceDep,
) -> ContactRead:
    """Give the customer a name, change it, or take it away with null (FEB-19).

    The only field: the channel's own name and the identities are not a
    person's to edit. Any member. The same value again changes nothing; a
    change is recorded in the audit log and re-read by open inboxes
    (`conversation.updated`, fields `participant`).
    """
    if payload.name == "":
        raise ValidationError(
            "A name cannot be empty. Send null to remove it.",
            error_code=CONTACT_NAME_INVALID,
        )
    view = await contacts.rename(contact_id, name=payload.name, actor=workspace.user)
    return ContactRead.from_view(view)


@router.get(
    "/{contact_id}/consent-events",
    response_model=CursorPage[ConsentEventRead],
    summary="A contact's consent history",
)
async def list_consent_events(
    contact_id: uuid.UUID,
    contacts: ContactServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PICKER_PAGE)] = MAX_PICKER_PAGE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> CursorPage[ConsentEventRead]:
    """Every opt-out and every clear of this contact, newest first (FEB-19).

    Any member. An opt-out recorded before the history was kept closes the
    list as one row marked `before_history`.
    """
    page = await contacts.consent_events(contact_id, limit=limit, cursor=cursor)
    return CursorPage[ConsentEventRead](
        items=[ConsentEventRead.from_view(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/{contact_id}/opt-out", summary="Record that a customer wants no campaigns")
async def record_opt_out(
    contact_id: uuid.UUID,
    payload: OptOutRequest,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
) -> ContactOptOutRead:
    """Take this person out of every future campaign audience on `channel`.

    Idempotent, and it never moves the timestamp forward: the first refusal is
    the one that matters, and a second one must not make it look freshly
    decided. Other channels are untouched (ENT-19).
    """
    contact = await campaigns.set_opt_out(
        contact_id=contact_id,
        channel=payload.channel,
        source=payload.source,
        actor_id=workspace.user.id,
    )
    identities, consents = await campaigns.opt_out_view(contact.id)
    return ContactOptOutRead.from_model(contact, identities=identities, consents=consents)


@router.delete("/{contact_id}/opt-out", summary="Let a customer receive campaigns again")
async def clear_opt_out(
    contact_id: uuid.UUID,
    channel: Annotated[Channel, Query(description="The channel to re-admit the person on.")],
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> ContactOptOutRead:
    """Undo an opt-out on `channel`, and on no other (ENT-19).

    Mostly for the case a colleague recorded one in error. Nothing automatic
    reaches this: a customer writing again after opting out is not re-enrolled.
    """
    contact = await campaigns.clear_opt_out(contact_id, channel=channel, actor_id=workspace.user.id)
    identities, consents = await campaigns.opt_out_view(contact.id)
    return ContactOptOutRead.from_model(contact, identities=identities, consents=consents)
