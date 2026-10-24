"""Reading and writing campaign header files (HM-02, ADR-144).

Two classes, the shape `media_repository` has. `CampaignHeaderMediaRepository`
is what a request and a campaign worker use: scoped to one workspace, so
another workspace's asset id answers not found. `PlatformCampaignMediaRepository`
is for the platform sweeps - upload reconciliation and retention - which read
every workspace's rows and are reachable from no request.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.media import MediaStorageState
from app.repositories.base import TenantScopedRepository

# The campaigns whose header file retention must keep (HM-07): anything that
# may still send. A completed, cancelled or failed campaign's file follows
# ordinary retention - a failed campaign is resumable, but resuming one whose
# file has been purged pauses it `header_media_unavailable`, which says why.
LIVE_CAMPAIGN_STATUSES: frozenset[CampaignStatus] = frozenset(
    {
        CampaignStatus.DRAFT,
        CampaignStatus.SCHEDULED,
        CampaignStatus.RUNNING,
        CampaignStatus.PAUSED,
    }
)


def _named_by_live_campaign() -> ColumnElement[bool]:
    # The campaign's own file, or one its rules froze on a recipient (HMR-03).
    own = exists().where(
        Campaign.tenant_id == CampaignHeaderMedia.tenant_id,
        Campaign.header_media_id == CampaignHeaderMedia.id,
        Campaign.status.in_(LIVE_CAMPAIGN_STATUSES),
    )
    recipients = exists().where(
        CampaignRecipient.tenant_id == CampaignHeaderMedia.tenant_id,
        CampaignRecipient.header_media_id == CampaignHeaderMedia.id,
        Campaign.tenant_id == CampaignRecipient.tenant_id,
        Campaign.id == CampaignRecipient.campaign_id,
        Campaign.status.in_(LIVE_CAMPAIGN_STATUSES),
    )
    return or_(own, recipients)


def _named_by_pending_follow_up() -> ColumnElement[bool]:
    # A follow-up template waiting to be sent with the file (HMT-03).
    return exists().where(
        FollowUp.tenant_id == CampaignHeaderMedia.tenant_id,
        FollowUp.header_media_id == CampaignHeaderMedia.id,
        FollowUp.status == FollowUpStatus.PENDING,
    )


class CampaignHeaderMediaRepository(TenantScopedRepository[CampaignHeaderMedia]):
    """One workspace's header files."""

    model = CampaignHeaderMedia

    def _tenant_filter(self) -> ColumnElement[bool]:
        return CampaignHeaderMedia.tenant_id == self.tenant_id

    async def get_by_id(self, asset_id: uuid.UUID) -> CampaignHeaderMedia | None:
        return await self._first(self._select().where(CampaignHeaderMedia.id == asset_id))

    async def require_by_id(self, asset_id: uuid.UUID) -> CampaignHeaderMedia:
        return await self._require(self._select().where(CampaignHeaderMedia.id == asset_id))

    async def lock_by_id(self, asset_id: uuid.UUID) -> CampaignHeaderMedia | None:
        """The row as it is now, locked: TX2 of the write protocol (ADR-087)."""
        result = await self._session.execute(
            self._select()
            .where(CampaignHeaderMedia.id == asset_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def by_ids(self, asset_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, CampaignHeaderMedia]:
        wanted = set(asset_ids)
        if not wanted:
            return {}
        rows = await self._all(self._select().where(CampaignHeaderMedia.id.in_(wanted)))
        return {row.id: row for row in rows}

    def create(
        self,
        *,
        kind: str,
        mime_type: str,
        byte_size: int,
        filename: str | None,
        content_hash: str,
        storage_key: str,
        upload_started_at: datetime,
        created_by_id: uuid.UUID | None,
    ) -> CampaignHeaderMedia:
        """Stage the intent: the key and what belongs at it, before the object exists."""
        return self.add(
            CampaignHeaderMedia(
                tenant_id=self.tenant_id,
                kind=kind,
                mime_type=mime_type,
                byte_size=byte_size,
                filename=filename,
                content_hash=content_hash,
                storage_key=storage_key,
                storage_state=MediaStorageState.PENDING,
                upload_started_at=upload_started_at,
                created_by_id=created_by_id,
            )
        )


class PlatformCampaignMediaRepository:
    """Every workspace's header files, for the platform sweeps only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_stale_uploads(
        self, *, cutoff: datetime, limit: int
    ) -> list[CampaignHeaderMedia]:
        """Intents older than the grace whose write never reported back (ADR-087)."""
        statement = (
            select(CampaignHeaderMedia)
            .where(
                CampaignHeaderMedia.storage_state == MediaStorageState.PENDING,
                CampaignHeaderMedia.upload_started_at <= cutoff,
            )
            .order_by(CampaignHeaderMedia.upload_started_at, CampaignHeaderMedia.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars())

    async def due_for_purge(self, *, cutoff: datetime, limit: int) -> list[CampaignHeaderMedia]:
        """Stored files past retention that no live campaign names (HM-07).

        Locked and skipped the way `message_media`'s sweep is. The live-campaign
        test is in the same statement as the lock, so a campaign composed on an
        old asset after this pass read it is the next pass's question - and a
        purged asset stops that campaign as `header_media_unavailable` rather
        than sending without its picture.
        """
        statement = (
            select(CampaignHeaderMedia)
            .where(
                CampaignHeaderMedia.storage_state == MediaStorageState.STORED,
                CampaignHeaderMedia.created_at < cutoff,
                ~_named_by_live_campaign(),
                ~_named_by_pending_follow_up(),
            )
            .order_by(CampaignHeaderMedia.created_at, CampaignHeaderMedia.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars())

    async def deleted_for_purge(self, *, limit: int) -> list[CampaignHeaderMedia]:
        """Stored files deleted from the template header library (HMT-01), whatever their age.

        Deleting is refused while anything live names a file; the same test is
        repeated here, in the statement that locks, for a file named after it
        was deleted by a writer that skipped that check.
        """
        statement = (
            select(CampaignHeaderMedia)
            .where(
                CampaignHeaderMedia.storage_state == MediaStorageState.STORED,
                CampaignHeaderMedia.deleted_at.is_not(None),
                ~_named_by_live_campaign(),
                ~_named_by_pending_follow_up(),
            )
            .order_by(CampaignHeaderMedia.deleted_at, CampaignHeaderMedia.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars())

    async def claimed_but_unfinished(self, *, limit: int) -> list[CampaignHeaderMedia]:
        statement = (
            select(CampaignHeaderMedia)
            .where(CampaignHeaderMedia.storage_state == MediaStorageState.PURGING)
            .order_by(CampaignHeaderMedia.purge_started_at, CampaignHeaderMedia.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars())

    async def pending_upload_count(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(CampaignHeaderMedia)
                .where(CampaignHeaderMedia.storage_state == MediaStorageState.PENDING)
            )
            or 0
        )

    async def mismatched_count(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(CampaignHeaderMedia)
                .where(CampaignHeaderMedia.storage_state == MediaStorageState.MISMATCHED)
            )
            or 0
        )

    async def pending_purge_count(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(CampaignHeaderMedia)
                .where(CampaignHeaderMedia.storage_state == MediaStorageState.PURGING)
            )
            or 0
        )


__all__ = [
    "LIVE_CAMPAIGN_STATUSES",
    "CampaignHeaderMediaRepository",
    "PlatformCampaignMediaRepository",
]
