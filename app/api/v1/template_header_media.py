"""The workspace's template header library (HMT-01, ADR-154).

The files a template's IMAGE, VIDEO or DOCUMENT header is sent with - by a
campaign, a person's template from the inbox, or a follow-up template. The same
upload as the campaign header file (`/campaigns/header-media`, kept), the same
`kind_for`: the file's own bytes decide its kind. Meta's id for a file never
leaves the server (HM-08).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    CampaignHeaderMediaServiceDep,
    TenantAdminDep,
)
from app.api.route import CommittingRoute
from app.api.uploads import read_capped
from app.core.dependencies import SessionDep, SettingsDep
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.campaign_media import HeaderMediaKind
from app.schemas.campaign import HeaderMediaRead, HeaderMediaUploadRead
from app.schemas.conversation import CursorPage
from app.services.template_header_media import TemplateHeaderMediaService

router = APIRouter(
    route_class=CommittingRoute, prefix="/template-header-media", tags=["Template header media"]
)

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]


def _library(
    session: SessionDep, settings: SettingsDep, workspace: ActiveWorkspaceDep
) -> TemplateHeaderMediaService:
    return TemplateHeaderMediaService(
        session,
        tenant_id=workspace.tenant.id,
        refresh=timedelta(seconds=settings.campaign_header_media_refresh_seconds),
    )


@router.post(
    "",
    response_model=HeaderMediaUploadRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a template header file",
)
async def upload_template_header_media(
    workspace: TenantAdminDep,
    header_media: CampaignHeaderMediaServiceDep,
    file: Annotated[UploadFile, File()],
) -> HeaderMediaUploadRead:
    """Owner or admin. An image, a video or a PDF for a template header (HMT-01).

    Multipart. The file's bytes decide its kind - JPEG / PNG image, MP4 / 3GPP
    video, PDF document; Meta's limit for the kind applies (image 5 MB, video and
    document 16 MB). It counts against the workspace's storage. Nothing goes to
    Meta here: the file is uploaded when something is sent with it.
    """
    content = await read_capped(file)
    asset = await header_media.upload(
        content=content,
        claimed_type=file.content_type,
        filename=file.filename,
        actor=workspace.user,
    )
    return HeaderMediaUploadRead.from_model(asset)


@router.get("", response_model=CursorPage[HeaderMediaRead], summary="The template header library")
async def list_template_header_media(
    session: SessionDep,
    settings: SettingsDep,
    workspace: ActiveWorkspaceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    kind: HeaderMediaKind | None = None,
) -> CursorPage[HeaderMediaRead]:
    """Any member. Newest first; `kind` narrows it to images, videos or documents."""
    page = await _library(session, settings, workspace).list(kind=kind, limit=limit, cursor=cursor)
    return CursorPage[HeaderMediaRead](
        items=[HeaderMediaRead.from_model(asset) for asset in page.items],
        next_cursor=page.next_cursor,
    )


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a template header file",
    responses={
        409: {"description": "header_media_in_use: a live campaign or a waiting follow-up uses it."}
    },
)
async def delete_template_header_media(
    asset_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    workspace: TenantAdminDep,
) -> Response:
    """Owner or admin. Refused 409 while a live campaign, one of its recipients or a waiting
    follow-up names the file; otherwise it leaves the library and its object is purged."""
    await _library(session, settings, workspace).delete(asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
