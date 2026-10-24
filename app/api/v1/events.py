"""The inbox's event stream (RT-01, RT-05, ADR-155).

`POST /events/ticket` (any member, workspace bucket) hands out a single-use
ticket; `GET /events/stream?ticket=` opens a `text/event-stream` with it -
EventSource cannot send an Authorization header, so the ticket is the
credential. The stream is exempt from the request timeout and from the
per-request rate buckets: it is one long request, bounded instead by
`RT_STREAMS_PER_USER` and `RT_STREAMS_PER_WORKSPACE` (429 `stream_limit`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import ActiveWorkspaceDep
from app.api.rate_limits import WorkspaceRateLimit
from app.api.route import CommittingRoute
from app.core.dependencies import RedisDep, SettingsDep
from app.core.exceptions import AuthenticationError, RateLimitedError
from app.schemas.events import EventTicketRead
from app.services import realtime_streams
from app.services.realtime import (
    EventStreamResponse,
    StreamHub,
    StreamSession,
    issue_ticket,
    new_stream_id,
    redeem_ticket,
    valid_event_id,
)

router = APIRouter(route_class=CommittingRoute, prefix="/events", tags=["Events"])

STREAM_LIMIT_CODE = "stream_limit"


def hub_of(request: Request, settings: SettingsDep) -> StreamHub:
    """This process's hub, made on first use."""
    hub = getattr(request.app.state, "event_hub", None)
    if hub is None:
        hub = StreamHub(
            request.app.state.redis.client,
            request.app.state.database,
            keepalive_seconds=settings.rt_keepalive_seconds,
        )
        request.app.state.event_hub = hub
    return hub


@router.post(
    "/ticket",
    response_model=EventTicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="A single-use ticket to open an event stream",
)
async def create_ticket(
    workspace: ActiveWorkspaceDep,
    redis: RedisDep,
    settings: SettingsDep,
    limit: WorkspaceRateLimit,
) -> EventTicketRead:
    """Any member. Valid `RT_TICKET_TTL_SECONDS` (60), once, for this person and workspace."""
    ticket, expires_at = await issue_ticket(
        redis.client,
        user_id=workspace.user.id,
        tenant_id=workspace.tenant.id,
        token_version=workspace.user.token_version,
        ttl=settings.rt_ticket_ttl_seconds,
    )
    return EventTicketRead(ticket=ticket, expires_at=expires_at)


@router.get(
    "/stream",
    summary="The workspace's event stream (text/event-stream)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "Events `message.created`, `message.status`, `conversation.updated`, "
                "`follow_up.updated`, `campaign.changed` (ids and hints, never content), "
                "`resync`, `closed`; a `: keep-alive` comment every 15 s; `retry: 3000`."
            ),
        },
        401: {"description": "The ticket is unknown, used, expired or no longer valid."},
        429: {"description": "stream_limit: too many open streams."},
    },
)
async def open_stream(
    request: Request,
    redis: RedisDep,
    settings: SettingsDep,
    ticket: Annotated[str, Query(max_length=200)],
    last_event_id_query: Annotated[str | None, Query(alias="lastEventId", max_length=40)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=40)] = None,
) -> StreamingResponse:
    """Open the stream with a ticket from `POST /events/ticket`.

    Reconnecting, the browser sends `Last-Event-ID`: the events after it are
    replayed when the workspace's stream still holds them, otherwise the first
    event is `resync` and the client re-reads with `updated_since`.
    """
    redeemed = await redeem_ticket(redis.client, ticket)
    if redeemed is None:
        raise AuthenticationError("This stream ticket is not valid. Ask for a new one.")
    session = StreamSession(
        ticket=redeemed,
        stream_id=new_stream_id(),
        database=request.app.state.database,
        redis=redis.client,
        hub=hub_of(request, settings),
        keepalive_seconds=settings.rt_keepalive_seconds,
        last_event_id=valid_event_id(last_event_id or last_event_id_query),
    )
    if await session.closing_reason() not in (None, "workspace_switched"):
        raise AuthenticationError("This stream ticket is no longer valid. Ask for a new one.")
    if not await realtime_streams.register(
        redis.client,
        stream_id=session.stream_id,
        user_id=redeemed.user_id,
        tenant_id=redeemed.tenant_id,
        per_user=settings.rt_streams_per_user,
        per_workspace=settings.rt_streams_per_workspace,
        keepalive_seconds=settings.rt_keepalive_seconds,
    ):
        raise RateLimitedError(
            "Too many event streams are open. Close one and try again.",
            error_code=STREAM_LIMIT_CODE,
        )
    return EventStreamResponse(
        session.frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
