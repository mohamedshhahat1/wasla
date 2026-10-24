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

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    ActiveWorkspace,
    ActiveWorkspaceDep,
    CampaignHeaderMediaServiceDep,
    CampaignPlannerDep,
    CampaignServiceDep,
    MediaStorageDep,
    MessagingServiceDep,
    TenantAdminDep,
)
from app.api.rate_limits import CHARGED_AFTER_ETAG, charge_deferred
from app.api.route import CommittingRoute
from app.api.uploads import read_capped
from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import ValidationError
from app.core.pagination import MAX_CURSOR_LENGTH
from app.core.telemetry import record_campaign_event
from app.core.timezones import local, local_midnight
from app.db.models.campaign import (
    MAX_CREATE_KEY_LENGTH,
    Campaign,
    CampaignStatus,
    RecipientReason,
    RecipientStatus,
)
from app.repositories.campaign_repository import ExportRow
from app.schemas.campaign import (
    AudiencePreviewRequest,
    AudiencePreviewResponse,
    AudienceRequest,
    CampaignActivityRead,
    CampaignCreateRequest,
    CampaignProgressItem,
    CampaignProgressRead,
    CampaignRead,
    CampaignScheduleRequest,
    CampaignStatisticsRead,
    CampaignSummaryRead,
    CampaignUpdateRequest,
    DeliveryPlanRead,
    EtaRead,
    HeaderMediaRead,
    HeaderMediaUploadRead,
    MessagePreviewRead,
    MessagePreviewRequest,
    MessagePreviewResponse,
    RecipientListResponse,
    RecipientRead,
    TestSendRequest,
)
from app.schemas.conversation import CursorPage, MessageRead
from app.schemas.rate_card import CostEstimateRead
from app.services.campaign_export import csv_header, csv_rows
from app.services.campaign_planner import CampaignPlanner
from app.services.campaign_service import (
    CAMPAIGN_VERSION_REQUIRED_CODE,
    PARAMETER_SOURCE_NOT_ALLOWED_CODE,
    CampaignService,
)
from app.services.cost_estimate import CampaignCostEstimator
from app.services.personalisation import ParameterSource

router = APIRouter(route_class=CommittingRoute, prefix="/campaigns", tags=["campaigns"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]
# The longest name search (PG-01).
MAX_SEARCH_LENGTH = 100
# A create's retry key (PG-08), the convention of a manual send's.
CreateKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=MAX_CREATE_KEY_LENGTH,
        description=(
            "An opaque key of your own. Repeating the create with the same key "
            "within a day returns the draft it made instead of a second one."
        ),
    ),
]


def _request_hash(payload: CampaignCreateRequest) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The progress route's bound and polling advice (PG-12).
MAX_PROGRESS_IDS = 20
POLL_LIVE_SECONDS = 10
POLL_IDLE_SECONDS = 60
# The summary's default window, in days (PG-11).
SUMMARY_DEFAULT_DAYS = 30
SUMMARY_WINDOW_NEEDS_OFFSET_CODE = "summary_window_needs_offset"
CSV_CHUNK_ROWS = 500


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _version(if_match: str) -> int:
    """A draft's version from `If-Match` - `3`, `"3"` or `W/"3"`."""
    raw = if_match.strip().removeprefix("W/").strip('"')
    if not raw.isdigit():
        raise ValidationError(
            'If-Match carries the draft\'s version, for example "3".',
            error_code=CAMPAIGN_VERSION_REQUIRED_CODE,
        )
    return int(raw)


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
    q: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH)] = None,
) -> CursorPage[CampaignRead]:
    """Newest first. `q` finds campaigns whose name contains it, ignoring case (PG-01)."""
    page = await campaigns.list_campaigns(
        statuses=tuple(status_filter or ()),
        account_id=account_id,
        limit=limit,
        cursor=cursor,
        name_contains=(q or "").strip() or None,
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
    idempotency_key: CreateKeyHeader = None,
) -> CampaignRead:
    """Create a draft. Nothing is sent and no audience exists yet.

    Send `Idempotency-Key` to make a retried request return the draft it
    already made (PG-08); the key reused for a different request is 409
    `idempotency_key_reused`.

    Body and text-header values are constants (`variables`, `header_variables`)
    or, per placeholder, a field of each person with a fallback
    (`body_parameters`, `header_parameters`; PER-01). Button values are always
    constants.
    """
    if any(entry.source is not ParameterSource.CONSTANT for entry in payload.button_parameters):
        raise ValidationError(
            "A button's value is the same for everybody: a personal value in a link leaves "
            "Wasla in a URL.",
            error_code=PARAMETER_SOURCE_NOT_ALLOWED_CODE,
        )
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
        header_media_mode=payload.header_media_mode.value,
        header_media_rules=(
            [rule.model_dump(mode="json") for rule in payload.header_media_rules]
            if payload.header_media_rules
            else None
        ),
        actor=workspace.user,
        body_mappings=(
            [entry.to_mapping() for entry in payload.body_parameters]
            if payload.body_parameters is not None
            else None
        ),
        header_mappings=(
            [entry.to_mapping() for entry in payload.header_parameters]
            if payload.header_parameters is not None
            else None
        ),
        idempotency_key=idempotency_key,
        request_hash=_request_hash(payload) if idempotency_key is not None else None,
    )
    return await _read(campaigns, campaign, workspace)


@router.patch("/{campaign_id}", response_model=CampaignRead, summary="Edit a draft")
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdateRequest,
    response: Response,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
    if_match: Annotated[str | None, Header(alias="If-Match", max_length=32)] = None,
) -> CampaignRead:
    """Change a draft's fields (PG-03). Drafts only: 409 `campaign_not_editable`.

    Name the version edited, as `If-Match` or `version`: an edit made from an
    older one is 409 `campaign_version_conflict` with the current version.
    A change to the number, template, values or header file clears the
    written list (`audience_cleared`); its rules stay in `audience`. Status,
    schedule and counters are never edited here: they have named transitions.
    """
    if payload.personal_buttons():
        raise ValidationError(
            "A button's value is the same for everybody: a personal value in a link leaves "
            "Wasla in a URL.",
            error_code=PARAMETER_SOURCE_NOT_ALLOWED_CODE,
        )
    campaign, cleared = await campaigns.update_draft(
        campaign_id,
        version=_version(if_match) if if_match is not None else payload.version,
        changes=payload.changes(),
        actor=workspace.user,
    )
    read = await _read(campaigns, campaign, workspace)
    read.audience_cleared = cleared
    response.headers["ETag"] = f'"{campaign.version}"'
    return read


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a draft")
async def delete_campaign(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> Response:
    """Delete a draft nobody was sent (PG-04); anything else is 409 `campaign_not_deletable`."""
    await campaigns.delete_draft(campaign_id, actor=workspace.user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{campaign_id}/duplicate",
    response_model=CampaignRead,
    status_code=status.HTTP_201_CREATED,
    summary="Copy a campaign into a new draft",
)
async def duplicate_campaign(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> CampaignRead:
    """A new draft with this campaign's composition and audience rules (PG-02).

    Never its written list, schedule or results. Validated like a create.
    """
    return await _read(
        campaigns, await campaigns.duplicate(campaign_id, actor=workspace.user), workspace
    )


@router.get(
    "/{campaign_id}/cost-estimate",
    response_model=CostEstimateRead,
    summary="What this campaign may cost at Meta's prices",
)
async def cost_estimate(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    session: SessionDep,
) -> CostEstimateRead:
    """An upper-bound estimate at the active rate card's marketing rates (COST-02).

    Everyone counted as delivered; Meta bills the business directly - Wasla
    charges nothing for messages. No active card: `total` null with `reason:
    no_rate_card`.
    """
    estimate = await CampaignCostEstimator(session, tenant_id=workspace.tenant.id).estimate(
        campaign_id
    )
    return CostEstimateRead.from_estimate(estimate)


@router.post(
    "/{campaign_id}/test-send",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a test to myself",
)
async def send_test(
    campaign_id: uuid.UUID,
    payload: TestSendRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
    messaging: MessagingServiceDep,
    storage: MediaStorageDep,
    settings: SettingsDep,
) -> MessageRead:
    """This campaign's message, as a recipient would get it, to one contact (PG-09).

    The contact must already have a conversation with the campaign's number -
    the admin writes to the business from their own phone first (422
    `contact_has_no_conversation`); opted out -> 422 `contact_opted_out`. Not
    a recipient, not in the statistics, not under the per-contact cap or quiet
    hours. At most `CAMPAIGN_TEST_SENDS_PER_DAY` per workspace in 24 hours
    (429 `test_send_limit`, `details.reset_at`). Any status but cancelled.
    """
    message = await campaigns.send_test(
        campaign_id,
        contact_id=payload.contact_id,
        actor=workspace.user,
        sender_role=workspace.role,
        messaging=messaging,
        storage=storage,
        per_day=settings.campaign_test_sends_per_day,
    )
    return MessageRead.from_model(message)


@router.get(
    "/{campaign_id}/activity",
    response_model=CursorPage[CampaignActivityRead],
    summary="What happened to a campaign",
)
async def campaign_activity(
    campaign_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> CursorPage[CampaignActivityRead]:
    """Newest first, from the audit trail (PG-05): people's actions and the system's.

    Field names, counts and reasons only - never a contact or a value.
    """
    page = await campaigns.activity(campaign_id, limit=limit, cursor=cursor)
    timezone = workspace.tenant.timezone
    return CursorPage[CampaignActivityRead](
        items=[CampaignActivityRead.from_entry(entry, timezone=timezone) for entry in page.items],
        next_cursor=page.next_cursor,
    )


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
        segment_id=payload.segment_id,
    )
    return AudiencePreviewResponse(account_id=payload.account_id, size=size)


@router.get("/summary", response_model=CampaignSummaryRead, summary="KPI cards")
async def campaigns_summary(
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    account_id: uuid.UUID | None = None,
) -> CampaignSummaryRead:
    """Every campaign created in the window, by aggregate SQL (PG-11).

    `from` / `to` need their UTC offsets; by default the last 30 days, from
    midnight in the workspace's own time zone, to now.
    """
    timezone = workspace.tenant.timezone
    now = datetime.now(UTC)
    for name, value in (("from", since), ("to", until)):
        if value is not None and value.utcoffset() is None:
            raise ValidationError(
                f"`{name}` needs a UTC offset, for example 2026-10-01T00:00:00+03:00.",
                error_code=SUMMARY_WINDOW_NEEDS_OFFSET_CODE,
            )
    start = since or local_midnight(now - timedelta(days=SUMMARY_DEFAULT_DAYS), timezone)
    end = until or now
    by_status, statistics = await campaigns.summary(since=start, until=end, account_id=account_id)
    return CampaignSummaryRead(
        campaigns_by_status=by_status,
        recipients={
            "sent": statistics.sent,
            "delivered": statistics.delivered,
            "read": statistics.read,
            "failed": statistics.failed,
            "skipped": statistics.skipped,
            "held": statistics.held,
            "replied": statistics.replied,
            "opted_out_after": statistics.opted_out_after,
            "pending": statistics.pending,
        },
        rates={
            "delivery": _rate(statistics.delivered, statistics.sent),
            "read": _rate(statistics.read, statistics.delivered),
            "reply": _rate(statistics.replied, statistics.sent),
        },
        window={
            "from": start,
            "to": end,
            "from_local": local(start, timezone),
            "to_local": local(end, timezone),
            "timezone": timezone,
        },
    )


@router.get(
    "/progress",
    response_model=CampaignProgressRead,
    summary="Live state of up to 20 campaigns",
    responses={304: {"description": "Nothing changed since the ETag sent"}},
)
async def campaigns_progress(
    request: Request,
    workspace: ActiveWorkspaceDep,
    campaigns: CampaignServiceDep,
    planner: CampaignPlannerDep,
    ids: Annotated[list[uuid.UUID], Query(min_length=1, max_length=MAX_PROGRESS_IDS)],
) -> Response:
    """One request for every campaign the page shows (PG-12).

    Poll every `poll_after_seconds` (10 while any is live, 60 otherwise) with
    the last `ETag` as `If-None-Match`: 304 when nothing changed. A 304 is
    counted in the workspace bucket only; the campaign bucket is charged for an
    answer that changed (RT-07). A page with an event stream open polls only
    after a `campaign.changed` hint, or every 60 s as a safety net.
    """
    found, counts = await campaigns.progress(ids)
    timezone = workspace.tenant.timezone
    now = datetime.now(UTC)
    items = []
    for campaign in found:
        eta = await planner.eta(campaign, now=now)
        items.append(
            CampaignProgressItem(
                id=campaign.id,
                status=campaign.status,
                waiting_reason=campaign.waiting_reason,
                resumes_at=campaign.resumes_at,
                resumes_at_local=(
                    local(campaign.resumes_at, timezone) if campaign.resumes_at else None
                ),
                counts=counts[campaign.id],
                eta=EtaRead.from_eta(eta, timezone=timezone),
                updated_at=campaign.updated_at,
            )
        )
    live_any = any(
        item.status in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING) for item in items
    )
    body = CampaignProgressRead(
        items=items, poll_after_seconds=POLL_LIVE_SECONDS if live_any else POLL_IDLE_SECONDS
    )
    # The ETag ignores the ETA's own clock-driven drift: it changes when a
    # count, a status or a wait changes.
    stable = [
        {key: value for key, value in item.model_dump(mode="json").items() if key != "eta"}
        for item in items
    ]
    tag = '"' + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:32] + '"'
    if request.headers.get("If-None-Match") == tag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": tag})
    await charge_deferred(request)
    return Response(
        content=body.model_dump_json(),
        media_type="application/json",
        headers={"ETag": tag, "Cache-Control": "no-cache"},
    )


# A 304 costs no campaign-bucket token (RT-07).
setattr(campaigns_progress, CHARGED_AFTER_ETAG, ("campaign",))


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


@router.post(
    "/{campaign_id}/preview-messages",
    response_model=MessagePreviewResponse,
    summary="What chosen people would receive",
)
async def preview_messages(
    campaign_id: uuid.UUID,
    payload: MessagePreviewRequest,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
) -> MessagePreviewResponse:
    """Up to five people's own messages, rendered (PER-04), and which values fell back.

    From the values written with the audience, or - before it is written - resolved
    now for people of the eligible population. Anybody else is not found; nothing
    about anybody but the people asked for is returned.
    """
    previews = await campaigns.preview_messages(campaign_id, payload.contact_ids)
    return MessagePreviewResponse(
        previews=[
            MessagePreviewRead(
                contact_id=preview.contact_id,
                header=preview.header,
                body=preview.body,
                buttons=list(preview.buttons),
                fallbacks={
                    "body": list(preview.body_fallbacks),
                    "header": list(preview.header_fallbacks),
                },
                header_media_id=preview.header_media_id,
            )
            for preview in previews
        ]
    )


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
        segment_id=payload.segment_id,
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
    planned = await planner.plan(campaign, now=datetime.now(UTC))
    read.delivery_plan = DeliveryPlanRead.from_plan(planned)
    if any(notice.value == "spreads_over_days" for notice in planned.notices):
        # A schedule Meta's limit spreads over days (LIM-04): how often the
        # limit, rather than the rate, decides when a campaign finishes.
        await record_campaign_event("plan_spread")
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


@router.get(
    "/{campaign_id}/recipients/export.csv",
    summary="Download the recipient list as CSV",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_recipients(
    campaign_id: uuid.UUID,
    workspace: TenantAdminDep,
    campaigns: CampaignServiceDep,
    settings: SettingsDep,
    status_filter: Annotated[RecipientStatus | None, Query(alias="status")] = None,
    reason: RecipientReason | None = None,
) -> StreamingResponse:
    """Every recipient as CSV, UTF-8 with a BOM so Excel opens Arabic names (PG-14).

    Columns: contact_name, phone, status, reason_code, provider_code, sent_at,
    delivered_at, read_at, replied_at, opted_out_after - times in the
    workspace's own zone with their offset. A cell that a spreadsheet would read
    as a formula is prefixed with `'`. Admin only; at most
    `CAMPAIGN_EXPORTS_PER_HOUR` per workspace (429 `export_limit`). Audited
    with the filters and the row count, never the rows.

    The rows are read in pages of 1,000 inside the request - while its
    transaction is open - and the body is streamed in chunks; a list is at most
    10,000 people.
    """
    campaign, pages = await campaigns.export_recipients(
        campaign_id,
        status=status_filter,
        reason=reason,
        actor=workspace.user,
        per_hour=settings.campaign_exports_per_hour,
    )
    chunks = [csv_header()]
    batch: list[ExportRow] = []
    async for row in pages():
        batch.append(row)
        if len(batch) == CSV_CHUNK_ROWS:
            chunks.append(csv_rows(batch, timezone=workspace.tenant.timezone))
            batch = []
    if batch:
        chunks.append(csv_rows(batch, timezone=workspace.tenant.timezone))

    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    filename = f"campaign-{campaign.id}-recipients.csv"
    return StreamingResponse(
        body(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
