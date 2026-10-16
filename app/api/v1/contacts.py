"""Contact endpoints.

A contact is created by the webhook from what Meta reports and has nothing a
person edits but the marketing opt-out, so this is not a CRUD resource and
deliberately does not read like one. The one listing is the campaigns page's
contact picker (PG-07): search by name or number, and - on a chosen number -
who a campaign could reach.

An opt-out is per channel (ENT-19): a STOP on WhatsApp covers every WhatsApp
number of the workspace and nothing on Instagram or Messenger, so both
operations name the channel, and the answer reads the consent channel by
channel.

The two operations have different weights, and the roles say so. **Recording**
an opt-out is any member's to do: the person handling the conversation is the
one a customer says "stop sending me these" to, and making them find an
administrator first is how the request gets lost. **Clearing** one takes an
administrator, because undoing somebody's own refusal is a decision a workspace
should have to make deliberately.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.dependencies import ActiveWorkspaceDep, CampaignServiceDep, TenantAdminDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.channel import Channel
from app.db.models.lead import LeadStatus
from app.repositories.contact_picker_repository import PickedContact
from app.schemas.campaign import ContactOptOutRead, OptOutRequest
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
    display_name: str | None
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

    `q`: digits search the WhatsApp number, anything else the contact's name
    or their active lead's. `account_id`: contacts with a conversation on that
    number, each with whether a campaign from it could reach them;
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
        contact_id=contact_id, channel=payload.channel, source=payload.source
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
    contact = await campaigns.clear_opt_out(contact_id, channel=channel)
    identities, consents = await campaigns.opt_out_view(contact.id)
    return ContactOptOutRead.from_model(contact, identities=identities, consents=consents)
