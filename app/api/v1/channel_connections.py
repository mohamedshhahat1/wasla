"""A workspace's channel connections, read without regard to their channel (ENT-07).

Read-only on purpose. A connection is made by its channel's own connect flow -
`POST /whatsapp/accounts` for a WhatsApp number - which runs the provider's
ownership checks and the channel capacity guard; there is no generic create
here that could make a connection for a channel with no adapter. Enabling,
disabling and releasing stay on the channel's own routes, through the guard.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import ActiveWorkspaceDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.db.models.channel import Channel
from app.schemas.channel_connection import ChannelConnectionRead
from app.services.channel_connection_service import ChannelConnectionService

router = APIRouter(
    route_class=CommittingRoute, prefix="/channel-connections", tags=["channel-connections"]
)


@router.get("", response_model=list[ChannelConnectionRead])
async def list_channel_connections(
    workspace: ActiveWorkspaceDep,
    session: SessionDep,
    settings: SettingsDep,
    channel: Annotated[Channel | None, Query(description="Only this channel's.")] = None,
) -> list[ChannelConnectionRead]:
    """Every connection the workspace holds - active and disabled - newest first.

    Open to any member, like the capacity it explains. Released connections are
    history and are not listed; their conversations stay readable in the inbox.
    """
    connections = await ChannelConnectionService(
        session,
        tenant_id=workspace.tenant.id,
        default_plan_code=settings.default_plan_code,
    ).list_connections(channel=channel)
    return [ChannelConnectionRead.from_model(row) for row in connections]
