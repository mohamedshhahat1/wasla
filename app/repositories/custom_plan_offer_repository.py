"""Data access for custom plan offers (ADR-114).

A workspace reads its own offers through the tenant-scoped repository, which
cannot express another workspace's rows. The platform's cross-tenant reader and
the expiry sweep use the separate unscoped class, so the exception is visible.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement

from app.db.models.custom_plan_offer import (
    OPEN_OFFER_STATUSES,
    CustomPlanOffer,
    CustomPlanOfferStatus,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


class CustomPlanOfferRepository(TenantScopedRepository[CustomPlanOffer]):
    """One workspace's custom plan offers."""

    model = CustomPlanOffer

    def _tenant_filter(self) -> ColumnElement[bool]:
        return CustomPlanOffer.tenant_id == self.tenant_id

    async def get_by_id(self, offer_id: uuid.UUID) -> CustomPlanOffer | None:
        return await self._first(self._select().where(CustomPlanOffer.id == offer_id))

    async def lock(self, offer_id: uuid.UUID) -> CustomPlanOffer | None:
        """One offer, locked for the change deciding it.

        Serialises accept, decline, cancel, expiry and activation of the same
        offer, so two of them can never both believe they moved it.
        """
        offer = await self._first(
            self._select().where(CustomPlanOffer.id == offer_id).with_for_update()
        )
        if offer is not None:
            await self.session.flush()
            await self.session.refresh(offer)
        return offer

    async def open_offer(self) -> CustomPlanOffer | None:
        """The one offer this workspace can still act on, if any."""
        return await self._first(
            self._select().where(
                CustomPlanOffer.status.in_([status.value for status in OPEN_OFFER_STATUSES])
            )
        )

    async def history(self, *, limit: int = 50) -> list[CustomPlanOffer]:
        """Newest first."""
        return await self._all(
            self._select().order_by(CustomPlanOffer.created_at.desc()).limit(limit)
        )


class PlatformCustomPlanOfferRepository(BaseRepository[CustomPlanOffer]):
    """Offers across every workspace - platform staff and the sweep only."""

    model = CustomPlanOffer

    async def get_by_id(self, offer_id: uuid.UUID) -> CustomPlanOffer | None:
        return await self._first(self._select().where(CustomPlanOffer.id == offer_id))

    async def for_tenant(self, tenant_id: uuid.UUID) -> list[CustomPlanOffer]:
        return await self._all(
            self._select()
            .where(CustomPlanOffer.tenant_id == tenant_id)
            .order_by(CustomPlanOffer.created_at.desc())
        )

    async def due_to_expire(self, *, at: datetime, limit: int) -> list[CustomPlanOffer]:
        """Open offers whose acceptance window has closed, oldest first."""
        return await self._all(
            self._select()
            .where(CustomPlanOffer.status.in_([status.value for status in OPEN_OFFER_STATUSES]))
            .where(CustomPlanOffer.expires_at.is_not(None))
            .where(CustomPlanOffer.expires_at <= at)
            .order_by(CustomPlanOffer.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )


__all__ = [
    "CustomPlanOfferRepository",
    "CustomPlanOfferStatus",
    "PlatformCustomPlanOfferRepository",
]
