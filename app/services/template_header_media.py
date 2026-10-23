"""A workspace's template header files, for a person's and a follow-up's template (HMT, ADR-154).

The campaign header file (HM-01) generalised: the same table, the same upload
route semantics and the same `kind_for` - the file's own bytes decide its kind
- now usable by a manual template from the inbox and by a person's template
follow-up as well as by a campaign. Nothing is renamed (HMT-01); the library
routes are `/template-header-media`.

**Wasla builds the header, never the client** (HMT-02). A request whose
`components` carry a media header parameter - an `id` or a `link` - is refused
`header_media_must_be_uploaded`: a raw provider id from a browser is a file
nobody checked, and a link is a URL Meta would fetch on the workspace's behalf.
The client names a library file; Wasla checks its workspace and its kind
against the template's header, uploads it to Meta once per (file, connection)
and reuses the id while it is fresh, and builds the header component with the
one component builder campaigns use (`header_file_component`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.pagination import Cursor, Page, paginate
from app.core.storage import MediaStorage
from app.core.telemetry import record_template_header_upload
from app.db.models.campaign import Campaign
from app.db.models.campaign_media import (
    CampaignHeaderMedia,
    HeaderMediaKind,
    TemplateHeaderUpload,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.repositories.campaign_media_repository import (
    LIVE_CAMPAIGN_STATUSES,
    CampaignHeaderMediaRepository,
)
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.campaign_header_media_service import CampaignHeaderMediaService
from app.services.template_parameters import (
    FILE_HEADER_FORMATS,
    HeaderFileParameter,
    header_file_component,
    parameter_spec,
)

HEADER_MEDIA_MUST_BE_UPLOADED_CODE: Final = "header_media_must_be_uploaded"
HEADER_MEDIA_REQUIRED_CODE: Final = "header_media_required"
HEADER_MEDIA_KIND_MISMATCH_CODE: Final = "header_media_kind_mismatch"
HEADER_MEDIA_IN_USE_CODE: Final = "header_media_in_use"

_FILE_PARAMETER_TYPES: Final = frozenset(kind.value for kind in HeaderMediaKind)


def carries_raw_media_header(components: list[dict[str, Any]] | None) -> bool:
    """Whether a client's components name a header file themselves (an id or a link)."""
    for component in components or ():
        if str(component.get("type", "")).lower() != "header":
            continue
        for parameter in component.get("parameters") or ():
            if isinstance(parameter, dict) and parameter.get("type") in _FILE_PARAMETER_TYPES:
                return True
    return False


@dataclass(frozen=True, slots=True)
class ResolvedHeader:
    """The library file a template is sent with, and the template's header format."""

    asset: CampaignHeaderMedia


class TemplateHeaderMediaService:
    """One workspace's template header library and its uploads to Meta."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        refresh: timedelta,
        storage: MediaStorage | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._refresh = refresh
        self._storage = storage
        self._assets = CampaignHeaderMediaRepository(session, tenant_id=tenant_id)
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)

    # --------------------------------------------------------------- choosing

    async def resolve(
        self,
        *,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None,
        header_media_id: uuid.UUID | None,
    ) -> CampaignHeaderMedia | None:
        """The file a template is sent with, checked; None when it needs none (HMT-02).

        Another workspace's file is not found (404 - its existence is never
        revealed); a file of another kind than the template's header is
        `header_media_kind_mismatch`; a media header with no file is
        `header_media_required`. A template the registry has never heard of
        cannot be checked for its header: a named file is taken as given.
        """
        if carries_raw_media_header(components):
            raise ValidationError(
                "Choose the header file from the template header library "
                "(header_media_id); Wasla uploads it to WhatsApp itself.",
                error_code=HEADER_MEDIA_MUST_BE_UPLOADED_CODE,
            )
        template = await self._templates.find_anywhere(name=template_name, language=language)
        spec = parameter_spec(template.components) if template is not None else None
        header_format = spec.header_format if spec is not None else None
        if header_media_id is None:
            if header_format in FILE_HEADER_FORMATS:
                raise ValidationError(
                    f"This template's header is a {str(header_format).lower()}: choose a file "
                    "for it (header_media_id).",
                    error_code=HEADER_MEDIA_REQUIRED_CODE,
                )
            return None
        asset = await self._assets.get_by_id(header_media_id)
        if asset is None or asset.deleted_at is not None:
            raise NotFoundError()
        if not asset.is_stored:
            raise ValidationError("That header file is still being stored. Try again shortly.")
        if template is not None and (
            header_format not in FILE_HEADER_FORMATS or str(header_format).lower() != asset.kind
        ):
            raise ValidationError(
                "That file is not the kind this template's header takes.",
                error_code=HEADER_MEDIA_KIND_MISMATCH_CODE,
                details={"header": (header_format or "none").lower(), "file": asset.kind},
            )
        return asset

    # --------------------------------------------------------------- sending

    async def provider_media_id(
        self,
        asset: CampaignHeaderMedia,
        *,
        connection_id: uuid.UUID,
        upload: Any,
        origin: str,
    ) -> str:
        """Meta's id for `asset` through `connection_id`: reused while fresh, else uploaded once.

        `upload` is the messaging service's `upload_header_file` - the one
        provider upload path. The upload is made with the session released.
        """
        found = await self._session.scalar(
            select(TemplateHeaderUpload).where(
                TemplateHeaderUpload.tenant_id == self._tenant_id,
                TemplateHeaderUpload.header_media_id == asset.id,
                TemplateHeaderUpload.account_id == connection_id,
                TemplateHeaderUpload.campaign_id.is_(None),
            )
        )
        now = datetime.now(UTC)
        if found is not None and now - found.uploaded_at < self._refresh:
            return found.provider_media_id
        if self._storage is None:  # pragma: no cover - every sender supplies one
            raise RuntimeError("Sending a template header file needs the media store.")
        loaded = await CampaignHeaderMediaService(
            session=self._session, tenant_id=self._tenant_id, storage=self._storage
        ).load_for_upload(asset)
        provider_media_id: str = await upload(
            connection_id=connection_id,
            content=loaded.content,
            mime_type=loaded.mime_type,
            filename=asset.filename or f"header.{asset.mime_type.rsplit('/', 1)[-1]}",
        )
        uploaded_at = datetime.now(UTC)
        await self._session.execute(
            insert(TemplateHeaderUpload)
            .values(
                tenant_id=self._tenant_id,
                header_media_id=asset.id,
                account_id=connection_id,
                campaign_id=None,
                provider_media_id=provider_media_id,
                uploaded_at=uploaded_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    TemplateHeaderUpload.tenant_id,
                    TemplateHeaderUpload.header_media_id,
                    TemplateHeaderUpload.account_id,
                ],
                index_where=TemplateHeaderUpload.campaign_id.is_(None),
                set_={"provider_media_id": provider_media_id, "uploaded_at": uploaded_at},
            )
        )
        await record_template_header_upload(origin)
        return provider_media_id

    @staticmethod
    def components_with_header(
        components: list[dict[str, Any]] | None,
        asset: CampaignHeaderMedia,
        provider_media_id: str,
    ) -> list[dict[str, Any]]:
        """The client's components with the header Wasla built in front of them."""
        header = header_file_component(
            HeaderFileParameter(
                kind=asset.kind,
                provider_media_id=provider_media_id,
                filename=asset.filename if asset.kind == HeaderMediaKind.DOCUMENT.value else None,
            )
        )
        return [header, *(components or [])]

    # --------------------------------------------------------------- the library

    async def list(
        self, *, kind: HeaderMediaKind | None, limit: int, cursor: str | None
    ) -> Page[CampaignHeaderMedia]:
        after = Cursor.decode(cursor) if cursor else None
        statement = select(CampaignHeaderMedia).where(
            CampaignHeaderMedia.tenant_id == self._tenant_id,
            CampaignHeaderMedia.deleted_at.is_(None),
            CampaignHeaderMedia.storage_key.is_not(None),
        )
        if kind is not None:
            statement = statement.where(CampaignHeaderMedia.kind == kind.value)
        if after is not None:
            statement = statement.where(
                (CampaignHeaderMedia.created_at < after.sort_value)
                | (
                    (CampaignHeaderMedia.created_at == after.sort_value)
                    & (CampaignHeaderMedia.id < after.id)
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        CampaignHeaderMedia.created_at.desc(), CampaignHeaderMedia.id.desc()
                    ).limit(limit + 1)
                )
            ).scalars()
        )
        return paginate(
            rows, limit=limit, key=lambda row: Cursor(sort_value=row.created_at, id=row.id)
        )

    async def delete(self, asset_id: uuid.UUID) -> CampaignHeaderMedia:
        """Delete a file from the library - refused 409 while anything live names it (HMT-01)."""
        asset = await self._assets.lock_by_id(asset_id)
        if asset is None or asset.deleted_at is not None:
            raise NotFoundError()
        if await self.in_use(asset.id):
            raise ConflictError(
                "A live campaign or a waiting follow-up uses this file.",
                error_code=HEADER_MEDIA_IN_USE_CODE,
            )
        asset.deleted_at = datetime.now(UTC)
        return asset

    async def in_use(self, asset_id: uuid.UUID) -> bool:
        """A live campaign, a recipient of one, or a waiting follow-up names the file."""
        campaign = exists().where(
            Campaign.tenant_id == self._tenant_id,
            Campaign.header_media_id == asset_id,
            Campaign.status.in_(LIVE_CAMPAIGN_STATUSES),
        )
        follow_up = exists().where(
            FollowUp.tenant_id == self._tenant_id,
            FollowUp.header_media_id == asset_id,
            FollowUp.status == FollowUpStatus.PENDING,
        )
        used = await self._session.scalar(select(or_(campaign, follow_up)))
        return bool(used)


__all__ = [
    "HEADER_MEDIA_IN_USE_CODE",
    "HEADER_MEDIA_KIND_MISMATCH_CODE",
    "HEADER_MEDIA_MUST_BE_UPLOADED_CODE",
    "HEADER_MEDIA_REQUIRED_CODE",
    "TemplateHeaderMediaService",
    "carries_raw_media_header",
]
