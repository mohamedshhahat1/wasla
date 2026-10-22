"""One workspace's saved campaign segments (PG-06)."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, func, select, update

from app.db.models.campaign import Campaign
from app.db.models.campaign_segment import CampaignSegment
from app.repositories.base import TenantScopedRepository


class CampaignSegmentRepository(TenantScopedRepository[CampaignSegment]):
    """Segments of one workspace; another workspace's are not found."""

    model = CampaignSegment

    def _tenant_filter(self) -> ColumnElement[bool]:
        return CampaignSegment.tenant_id == self.tenant_id

    async def list_all(self) -> list[CampaignSegment]:
        """Every segment, by name: there are at most a hundred."""
        return await self._all(self._select().order_by(CampaignSegment.name, CampaignSegment.id))

    async def require_by_id(self, segment_id: uuid.UUID) -> CampaignSegment:
        return await self._require(self._select().where(CampaignSegment.id == segment_id))

    async def lock_by_id(self, segment_id: uuid.UUID) -> CampaignSegment:
        return await self._require(
            self._select()
            .where(CampaignSegment.id == segment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def count(self) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(CampaignSegment).where(self._tenant_filter())
            )
            or 0
        )

    async def name_taken(self, name: str, *, other_than: uuid.UUID | None = None) -> bool:
        statement = select(CampaignSegment.id).where(
            self._tenant_filter(), func.lower(CampaignSegment.name) == name.lower()
        )
        if other_than is not None:
            statement = statement.where(CampaignSegment.id != other_than)
        return await self.session.scalar(statement.limit(1)) is not None

    async def forget_on_campaigns(self, segment_id: uuid.UUID) -> int:
        """Clear the reference on this workspace's campaigns; their copied rules stay."""
        result = await self.session.execute(
            update(Campaign)
            .where(Campaign.tenant_id == self.tenant_id, Campaign.audience_segment_id == segment_id)
            .values(audience_segment_id=None)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = ["CampaignSegmentRepository"]
