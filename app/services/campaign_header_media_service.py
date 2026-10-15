"""A workspace's campaign header files: uploaded, stored, read back (HM-01 .. HM-03).

**Uploaded through Wasla, never linked** (HM-01). The bucket is private and no
link to it is ever issued for WhatsApp (MEDIA.md), so a header file reaches
Meta one way: the campaign worker reads it here and uploads the bytes, and
Meta sees a media id. Nothing a customer types becomes a URL Meta fetches.

**The bytes decide the kind** (HM-03, ADR-076). `media_types.resolve` reads the
file's own prefix; a browser's `content_type` is a hint that may only agree.
JPEG and PNG are an image, MP4 and 3GPP a video, PDF a document - Meta's
header formats, narrowed to what Meta accepts for each, and within Meta's own
size limits for the family (the WhatsApp policy's `media_limits`, OMNI-045),
under the route's `MAX_UPLOAD_BYTES`.

**Stored by the one write protocol** (ADR-087): the workspace's storage
capacity is reserved under its advisory lock and the intent - key, size,
SHA-256 - committed in the same transaction (TX1); the object is written with
no transaction held; the row is re-read under its lock and marked `stored`
(TX2). An interrupted write is left `pending` for upload reconciliation.

**Read back verified** (HM-06). The worker reads the object and recomputes its
SHA-256 before Meta is given anything: a file that is gone, or is not what was
stored, is `HeaderMediaUnavailableError`, and the campaign pauses rather than
sending a header Wasla cannot vouch for. A store that cannot be reached is
`StorageError` - it decides nothing, exactly as in reconciliation.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.policy import PolicyRefusalError, require_sendable_media
from app.core.exceptions import (
    DependencyUnavailableError,
    NotFoundError,
    PlanLimitExceededError,
    ValidationError,
)
from app.core.filenames import require_storable_filename
from app.core.logging import get_logger
from app.core.media_types import SNIFF_BYTES
from app.core.media_types import resolve as resolve_media_type
from app.core.storage import MediaStorage, StorageError, build_key, write_object
from app.db.models.audit import AuditAction
from app.db.models.billing import LimitKey
from app.db.models.campaign_media import CampaignHeaderMedia, HeaderMediaKind
from app.db.models.media import MediaStorageState
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.db.session import released
from app.integrations.whatsapp.policy import WhatsAppChannelPolicy
from app.repositories.campaign_media_repository import CampaignHeaderMediaRepository
from app.services.audit_service import AuditTrail
from app.services.entitlement_service import EntitlementService
from app.services.media_service import content_hash
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

#: Stable error codes an upload is refused with.
HEADER_MEDIA_TYPE: Final = "header_media_type"
HEADER_MEDIA_TOO_LARGE: Final = "header_media_too_large"
HEADER_MEDIA_NOT_STORED: Final = "header_media_not_stored"

#: Which canonical types fill which header format (HM-03). Meta's: "Media"
#: lists JPEG and PNG images and MP4 and 3GPP video; a template DOCUMENT header
#: is "a document such as a PDF", and Wasla takes PDF only.
KIND_BY_TYPE: Final[dict[str, HeaderMediaKind]] = {
    "image/jpeg": HeaderMediaKind.IMAGE,
    "image/png": HeaderMediaKind.IMAGE,
    "video/mp4": HeaderMediaKind.VIDEO,
    "video/3gpp": HeaderMediaKind.VIDEO,
    "application/pdf": HeaderMediaKind.DOCUMENT,
}


class HeaderMediaUnavailableError(Exception):
    """The stored file is gone, or is not the file that was stored (HM-06)."""


@dataclass(frozen=True, slots=True)
class HeaderFile:
    """What Meta is uploaded: the verified bytes, their type and a safe name."""

    kind: HeaderMediaKind
    content: bytes
    mime_type: str
    filename: str | None


def kind_for(mime_type: str) -> HeaderMediaKind:
    """The header format a canonical type can fill, or a 422 naming the formats."""
    kind = KIND_BY_TYPE.get(mime_type)
    if kind is None:
        raise ValidationError(
            "A campaign header file must be a JPEG or PNG image, an MP4 or 3GPP video, "
            "or a PDF document.",
            error_code=HEADER_MEDIA_TYPE,
        )
    return kind


class CampaignHeaderMediaService:
    """One workspace's header files."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        storage: MediaStorage,
        entitlements: EntitlementService | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._storage = storage
        self._entitlements = entitlements
        self._assets = CampaignHeaderMediaRepository(session, tenant_id=tenant_id)

    async def get(self, asset_id: uuid.UUID) -> CampaignHeaderMedia:
        return await self._assets.require_by_id(asset_id)

    async def upload(
        self,
        *,
        content: bytes,
        claimed_type: str | None,
        filename: str | None,
        actor: User,
    ) -> CampaignHeaderMedia:
        """Store one header file, refusing it before anything is written if it cannot be one.

        Commits mid-request, twice, as the outbound attachment path does: the
        intent before the object, the outcome after it (ADR-087).
        """
        if not content:
            raise ValidationError("The uploaded file is empty.")
        detected = resolve_media_type(claimed=claimed_type, prefix=content[:SNIFF_BYTES])
        mime_type = detected.mime_type
        kind = kind_for(mime_type)
        try:
            require_sendable_media(
                family=kind.value,
                mime_type=mime_type,
                byte_size=len(content),
                policy=_WHATSAPP_POLICY,
            )
        except PolicyRefusalError as error:
            raise ValidationError(error.message, error_code=HEADER_MEDIA_TOO_LARGE) from error
        # Meta shows a document's name to the customer; an image or a video
        # carries none. Refused, never repaired, if it cannot be stored (MEDIA-06).
        display_name = (
            require_storable_filename(filename) if kind is HeaderMediaKind.DOCUMENT else None
        )

        if self._entitlements is None:  # pragma: no cover - the route always supplies one
            raise RuntimeError("Uploading a header file needs the workspace's entitlements.")
        # TX1: the claim on the workspace's storage and the row that occupies
        # it, under the same lock and in the same transaction (ADR-091).
        capacity = await self._entitlements.reserve(LimitKey.STORAGE_BYTES, additional=len(content))
        if not capacity.allowed:
            raise PlanLimitExceededError("This workspace has no storage left for this file.")
        asset = self._assets.create(
            kind=kind.value,
            mime_type=mime_type,
            byte_size=len(content),
            filename=display_name,
            content_hash=content_hash(content),
            storage_key=build_key(tenant_id=self._tenant_id, mime_type=mime_type),
            upload_started_at=datetime.now(UTC),
            created_by_id=actor.id,
        )
        await self._session.flush()
        asset_id = asset.id
        key = asset.storage_key
        assert key is not None  # noqa: S101 - set two lines up

        async with released(self._session):
            written = await write_object(self._storage, key=key, data=content, mime_type=mime_type)
        if not written:
            # The intent stands, `pending`: reconciliation asks the store
            # whether the object arrived anyway and settles the row.
            logger.warning(
                "campaign.header_media_not_stored",
                extra={
                    "event": "campaign.header_media_not_stored",
                    "tenant_id": str(self._tenant_id),
                    "asset_id": str(asset_id),
                },
            )
            raise DependencyUnavailableError("The file could not be stored. Try again.")

        # TX2: still ours? A workspace purged between the two commits took the
        # row; the object it would have named is removed at once.
        locked = await self._assets.lock_by_id(asset_id)
        if locked is None:
            # Best effort: a refused delete leaves an object no row names, the
            # same outcome a purge racing an inbound write already accepts.
            with suppress(StorageError):
                await self._storage.delete(key)
            raise NotFoundError()
        locked.storage_state = MediaStorageState.STORED
        UsageRecorder(self._session, tenant_id=self._tenant_id).record(
            UsageEventType.STORAGE_USED,
            quantity=locked.byte_size,
            meta={"campaign_header_media_id": str(locked.id)},
        )
        AuditTrail(self._session, tenant_id=self._tenant_id).record(
            AuditAction.CAMPAIGN_HEADER_MEDIA_UPLOADED,
            actor=actor,
            target_type="campaign_header_media",
            target_id=locked.id,
            # Internal facts only: never the name, the key or the bytes.
            meta={"kind": locked.kind, "byte_size": locked.byte_size},
        )
        await self._session.flush()
        logger.info(
            "campaign.header_media_stored",
            extra={
                "event": "campaign.header_media_stored",
                "tenant_id": str(self._tenant_id),
                "asset_id": str(locked.id),
                "kind": locked.kind,
            },
        )
        return locked

    async def read(self, asset: CampaignHeaderMedia) -> bytes:
        """The stored bytes, for a colleague: only a stored file is served."""
        if asset.storage_state is not MediaStorageState.STORED or asset.storage_key is None:
            raise NotFoundError("This file is not stored.", error_code=HEADER_MEDIA_NOT_STORED)
        return await self._storage.get(asset.storage_key)

    async def load_for_upload(self, asset: CampaignHeaderMedia) -> HeaderFile:
        """The file Meta is to be given, verified against the hash committed with it.

        `HeaderMediaUnavailableError` when the file is gone or is not what was
        stored; `StorageError` when the store cannot say. HEAD first, as
        reconciliation does, because only it tells "absent" from "unreachable".
        """
        key = asset.storage_key
        if asset.storage_state is not MediaStorageState.STORED or key is None:
            raise HeaderMediaUnavailableError()
        if not await self._storage.exists(key):
            raise HeaderMediaUnavailableError()
        content = await self._storage.get(key)
        if len(content) != asset.byte_size or content_hash(content) != asset.content_hash:
            raise HeaderMediaUnavailableError()
        return HeaderFile(
            kind=HeaderMediaKind(asset.kind),
            content=content,
            mime_type=asset.mime_type,
            filename=asset.filename,
        )


#: The channel whose limits a header file is held to: campaigns send on WhatsApp.
_WHATSAPP_POLICY: Final = WhatsAppChannelPolicy()


__all__ = [
    "HEADER_MEDIA_NOT_STORED",
    "HEADER_MEDIA_TOO_LARGE",
    "HEADER_MEDIA_TYPE",
    "KIND_BY_TYPE",
    "CampaignHeaderMediaService",
    "HeaderFile",
    "HeaderMediaUnavailableError",
    "kind_for",
]
