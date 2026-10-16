"""Campaign endpoints.

Reading is open to any member — a person handling the inbox needs to know what
was sent to the customer in front of them. Everything that composes, targets or
starts a campaign takes an administrator, because a campaign writes to thousands
of customers at once and is the least reversible thing this platform does.

Note the shape of the lifecycle routes: `schedule`, `pause` and `cancel` are
named transitions rather than a general PATCH on `status`. Status is the only
field with an operational meaning, and naming the transition keeps both the
audit trail and the authorization legible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.api.dependencies import (
    ActiveWorkspace,
    ActiveWorkspaceDep,
    CampaignHeaderMediaServiceDep,
    CampaignPlannerDep,
    CampaignServiceDep,
    TenantAdminDep,
)
from app.api.route import CommittingRoute
from app.api.uploads import read_capped
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.campaign import Campaign, CampaignStatus, RecipientReason, RecipientStatus
from app.schemas.campaign import (
    AudiencePreviewRequest,
    AudiencePreviewResponse,
    AudienceRequest,
    CampaignCreateRequest,
    CampaignRead,
    CampaignScheduleRequest,
    CampaignStatisticsRead,
    DeliveryPlanRead,
    HeaderMediaRead,
    HeaderMediaUploadRead,
    RecipientListResponse,
    RecipientRead,
)
from app.schemas.conversation import CursorPage
from app.services.campaign_planner import CampaignPlanner
from app.services.campaign_service import CampaignService

router = APIRouter(route_class=CommittingRoute, prefix="/campaigns", tags=["campaigns"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]


async def _read(
    campaigns: CampaignService,
    campaign: Campaign,
    workspace: ActiveWorkspace,
    planner: CampaignPlanner | None = None,
) -> CampaignRead:
    """One campaign as the API shows it, with its header file if it has one (HM-08).

    Its times in the workspace's own zone too (TZ-03), and its ETA (PG-13).
    """
    files = await campaigns.header_media_for([campaign])
    eta = await planner.eta(campaign, now=datetime.now(UTC)) if planner is not None else None
    return CampaignRead.from_model(
        campaign,
        header_media=files.get(campaign.id),
        timezone=workspace.tenant.timezone,
        eta=eta,
    )


@router.get("", response_model=CursorPage[CampaignRead], summary="List campaigns")
async def list_campaigns(
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    planner: CampaignPlannerDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    status_filter: Annotated[list[CampaignStatus] | None, Query(alias="status")] = None,
    account_id: uuid.UUID | None = None,
) -> CursorPage[CampaignRead]:
    """Newest first."""
    page = await campaigns.list_campaigns(
        statuses=tuple(status_filter or ()),
        account_id=account_id,
        limit=limit,
        cursor=cursor,
    )
    files = await campaigns.header_media_for(page.items)
    now = datetime.now(UTC)
    # The ETA of the live ones only (PG-13): the rest have none to compute.
    etas = {
        campaign.id: await planner.eta(campaign, now=now)
        for campaign in page.items
        if campaign.status in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING)
    }
    return CursorPage[CampaignRead](
        items=[
            CampaignRead.from_model(
                campaign,
                header_media=files.get(campaign.id),
                timezone=workspace.tenant.timezone,
                eta=etas.get(campaign.id),
            )
            for campaign in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.post(
    "",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
    summary="Compose a campaign",
)
async def create_campaign(
    payload: CampaignCreateRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> CampaignRead:
    """Create a draft. Nothing is sent and no audience exists yet."""
    campaign = await campaigns.create(
        account_id=payload.account_id,
        template_id=payload.template_id,
        name=payload.name,
        description=payload.description,
        variables=list(payload.variables),
        messages_per_minute=payload.messages_per_minute,
        created_by_id=workspace.user.id,
        header_variables=list(payload.header_variables),
        button_parameters={entry.index: entry.value for entry in payload.button_parameters},
        header_media_id=payload.header_media_id,
        actor=workspace.user,
    )
    return await _read(campaigns, campaign, workspace)


@router.post(
    "/header-media",
    response_model=HeaderMediaUploadRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a template header file",
)
async def upload_header_media(
    workspace: TenantAdminDep,
    header_media: CampaignHeaderMediaServiceDep,
    file: Annotated[UploadFile, File()],
) -> HeaderMediaUploadRead:
    """Store an image, a video or a PDF a campaign's template header is sent with (HM-02).

    Multipart, like an attachment. The file's own bytes decide its kind - a
    JPEG or PNG image, an MP4 or 3GPP video, a PDF document - whatever the
    browser said; Meta's limit for the kind applies (image 5 MB, video and
    document 16 MB). It counts against the workspace's storage. Nothing is
    sent to Meta here: the campaign worker uploads it once per campaign.
    """
    content = await read_capped(file)
    asset = await header_media.upload(
        content=content,
        claimed_type=file.content_type,
        filename=file.filename,
        actor=workspace.user,
    )
    return HeaderMediaUploadRead.from_model(asset)


@router.get("/header-media/{asset_id}", response_model=HeaderMediaRead, summary="One header file")
async def get_header_media(
    asset_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    header_media: CampaignHeaderMediaServiceDep,
) -> HeaderMediaRead:
    return HeaderMediaRead.from_model(await header_media.get(asset_id))


@router.get("/header-media/{asset_id}/file", summary="A header file's bytes")
async def download_header_media(
    asset_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    header_media: CampaignHeaderMediaServiceDep,
) -> Response:
    """The stored bytes, through the authenticated API and never from a link (HM-01)."""
    asset = await header_media.get(asset_id)
    content = await header_media.read(asset)
    return Response(
        content=content,
        # Canonical: resolved from the bytes when the file was uploaded.
        media_type=asset.mime_type,
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# Declared before `/{campaign_id}` so the literal path is matched first. A UUID
# path parameter would otherwise reject "audience" as malformed before this
# route was ever considered.
@router.post("/audience/preview", summary="Count who a filter would reach")
async def preview_audience(
    payload: AudiencePreviewRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> AudiencePreviewResponse:
    """How many people this filter reaches, without writing anything.

    The count is of contacts who already have a conversation on this number and
    have not opted out. There is no way to widen it.
    """
    size = await campaigns.preview_audience(
        account_id=payload.account_id,
        filters=payload.to_filter(),
    )
    return AudiencePreviewResponse(account_id=payload.account_id, size=size)


@router.get("/{campaign_id}", response_model=CampaignRead, summary="One campaign")
async def get_campaign(
    campaign_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    planner: CampaignPlannerDep,
) -> CampaignRead:
    return await _read(campaigns, await campaigns.get(campaign_id), workspace, planner)


@router.get(
    "/{campaign_id}/delivery-plan",
    response_model=DeliveryPlanRead,
    summary="When the campaign is expected to reach its people",
)
async def delivery_plan(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
    planner: CampaignPlannerDep,
) -> DeliveryPlanRead:
    """The plan, recomputed now (LIM-05): days, first and last send, completion, notices.

    From the campaign's rate, the workspace's quiet hours, the portfolio's
    campaign budget and what is already used of it, and per-person deferrals.
    An estimate: other senders on the portfolio, a changed limit and the rate
    actually achieved all move it. A draft's plan starts now.
    """
    campaign = await campaigns.get(campaign_id)
    return DeliveryPlanRead.from_plan(await planner.plan(campaign, now=datetime.now(UTC)))


@router.post("/{campaign_id}/audience", response_model=CampaignRead, summary="Set the audience")
async def set_audience(
    campaign_id: uuid.UUID,
    payload: AudienceRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> CampaignRead:
    """Write out the recipient list.

    Drafts only, and the list is materialised now rather than computed at send
    time — so the campaign can answer "who was this sent to" afterwards.
    """
    campaign = await campaigns.set_audience(
        campaign_id=campaign_id,
        filters=payload.to_filter(),
        actor=workspace.user,
    )
    return await _read(campaigns, campaign, workspace)


@router.post("/{campaign_id}/schedule", response_model=CampaignRead, summary="Send, now or later")
async def schedule_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignScheduleRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
    planner: CampaignPlannerDep,
) -> CampaignRead:
    """Hand the campaign to the worker. Omitting the time means now.

    Even "now" returns immediately: the worker does the sending, so no request
    is ever held open behind ten thousand messages.

    Meta's messaging limit for the sending number is read again first when
    the stored value is older than six hours (LIM-01); a number whose limit
    was never read is refused, 422 `messaging_limit_unknown`.
    """
    await campaigns.refresh_messaging_limit(campaign_id)
    campaign = await campaigns.schedule(
        campaign_id=campaign_id,
        scheduled_at=payload.scheduled_at,
        actor=workspace.user,
        send_until=payload.send_until,
    )
    read = await _read(campaigns, campaign, workspace, planner)
    # The plan comes with the answer (LIM-05): nothing is refused because of
    # it - spreading over days is the decided behaviour - but the page shows
    # when the first and the last person will be reached, and why.
    read.delivery_plan = DeliveryPlanRead.from_plan(
        await planner.plan(campaign, now=datetime.now(UTC))
    )
    return read


@router.post("/{campaign_id}/pause", response_model=CampaignRead, summary="Stop, reversibly")
async def pause_campaign(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> CampaignRead:
    """Stop sending and keep the rest of the list. Schedule again to resume."""
    return await _read(
        campaigns, await campaigns.pause(campaign_id, actor=workspace.user), workspace
    )


@router.post("/{campaign_id}/cancel", response_model=CampaignRead, summary="Stop, finally")
async def cancel_campaign(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> CampaignRead:
    """Finish the campaign where it stands. What has been sent stays sent."""
    return await _read(
        campaigns, await campaigns.cancel(campaign_id, actor=workspace.user), workspace
    )


@router.get("/{campaign_id}/statistics", summary="Delivery and failure counts")
async def campaign_statistics(
    campaign_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
) -> CampaignStatisticsRead:
    """`delivered` and `read` come from Meta's own status webhooks, not from us."""
    return CampaignStatisticsRead.from_statistics(await campaigns.statistics(campaign_id))


@router.get("/{campaign_id}/recipients", summary="Who a campaign reached")
async def list_recipients(
    campaign_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    status_filter: Annotated[RecipientStatus | None, Query(alias="status")] = None,
    reason: RecipientReason | None = None,
    limit: LimitQuery = 100,
    cursor: CursorQuery = None,
) -> RecipientListResponse:
    """Every recipient, a page at a time: follow `next_cursor` to the end (CMP-13)."""
    page = await campaigns.list_recipients(
        campaign_id, status=status_filter, reason=reason, limit=limit, cursor=cursor
    )
    return RecipientListResponse(
        recipients=[RecipientRead.from_model(row) for row in page.items],
        next_cursor=page.next_cursor,
    )
