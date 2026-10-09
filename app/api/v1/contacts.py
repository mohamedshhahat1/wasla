"""Contact endpoints.

Only the marketing opt-out lives here for now. A contact is created by the
webhook from what Meta reports and has nothing else a person edits, so this is
not a CRUD resource and deliberately does not read like one.

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
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import ActiveWorkspaceDep, CampaignServiceDep, TenantAdminDep
from app.api.route import CommittingRoute
from app.db.models.channel import Channel
from app.schemas.campaign import ContactOptOutRead, OptOutRequest

router = APIRouter(route_class=CommittingRoute, prefix="/contacts", tags=["contacts"])


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
