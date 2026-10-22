"""Saved campaign segments (PG-06).

Any member reads them - the people choosing an audience are the people who
need to see what "wrote in 30 days" means here. Creating, editing and deleting
take an administrator, as composing a campaign does. Counted in the campaign
rate bucket with the campaigns page's other routes.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import ActiveWorkspaceDep, CampaignSegmentServiceDep, TenantAdminDep
from app.api.route import CommittingRoute
from app.core.exceptions import ValidationError
from app.schemas.campaign_segment import (
    SegmentCreateRequest,
    SegmentListResponse,
    SegmentRead,
    SegmentUpdateRequest,
)
from app.services.campaign_segment_service import SEGMENT_VERSION_REQUIRED_CODE

router = APIRouter(
    route_class=CommittingRoute, prefix="/campaign-segments", tags=["campaign segments"]
)


def _version(if_match: str) -> int:
    raw = if_match.strip().removeprefix("W/").strip('"')
    if not raw.isdigit():
        raise ValidationError(
            'If-Match carries the segment\'s version, for example "3".',
            error_code=SEGMENT_VERSION_REQUIRED_CODE,
        )
    return int(raw)


@router.get("", response_model=SegmentListResponse, summary="Saved segments")
async def list_segments(
    workspace: ActiveWorkspaceDep, segments: CampaignSegmentServiceDep
) -> SegmentListResponse:
    """Every segment of the workspace, by name (at most 100)."""
    return SegmentListResponse(
        items=[SegmentRead.from_model(segment) for segment in await segments.list()]
    )


@router.get("/{segment_id}", response_model=SegmentRead, summary="One segment")
async def get_segment(
    segment_id: uuid.UUID, workspace: ActiveWorkspaceDep, segments: CampaignSegmentServiceDep
) -> SegmentRead:
    return SegmentRead.from_model(await segments.get(segment_id))


@router.post(
    "",
    response_model=SegmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save audience rules as a segment",
)
async def create_segment(
    payload: SegmentCreateRequest, workspace: TenantAdminDep, segments: CampaignSegmentServiceDep
) -> SegmentRead:
    """Rules only - recency and lead statuses; contact ids are refused (422).

    Names are unique in the workspace ignoring case (409 `segment_name_taken`);
    at most 100 segments (409 `segment_limit_reached`).
    """
    segment = await segments.create(
        name=payload.name,
        description=payload.description,
        rules=payload.rules.to_filter(),
        account_id=payload.account_id,
        actor=workspace.user,
    )
    return SegmentRead.from_model(segment)


@router.patch("/{segment_id}", response_model=SegmentRead, summary="Edit a segment")
async def update_segment(
    segment_id: uuid.UUID,
    payload: SegmentUpdateRequest,
    response: Response,
    workspace: TenantAdminDep,
    segments: CampaignSegmentServiceDep,
    if_match: Annotated[str | None, Header(alias="If-Match", max_length=32)] = None,
) -> SegmentRead:
    """From the version read (If-Match or `version`). Campaigns that used it are unchanged."""
    segment = await segments.update(
        segment_id,
        version=_version(if_match) if if_match is not None else payload.version,
        changes=payload.changes(),
    )
    response.headers["ETag"] = f'"{segment.version}"'
    return SegmentRead.from_model(segment)


@router.delete("/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a segment")
async def delete_segment(
    segment_id: uuid.UUID, workspace: TenantAdminDep, segments: CampaignSegmentServiceDep
) -> Response:
    """Campaigns that used it keep the rules they copied."""
    await segments.delete(segment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
