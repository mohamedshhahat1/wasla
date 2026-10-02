"""Data access for capacity reductions and owners' pre-selections (ENT-14, ENT-15).

Tenant-scoped for everything a workspace does - its open reduction, its
selection - and one unscoped reader for the billing worker, which finds the
reductions whose grace has ended or whose owners are still to be told, across
the deployment, like every other sweep.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import ChannelConnection, ConnectionStatus
from app.db.models.channel_capacity import (
    CapacityReductionStatus,
    ChannelCapacityPreselection,
    ChannelCapacityReduction,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


class ChannelCapacityReductionRepository(TenantScopedRepository[ChannelCapacityReduction]):
    """One workspace's reductions."""

    model = ChannelCapacityReduction

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ChannelCapacityReduction.tenant_id == self.tenant_id

    async def open(self, *, lock: bool = False) -> ChannelCapacityReduction | None:
        """The workspace's open reduction - at most one, by a partial unique index."""
        statement = self._select().where(
            ChannelCapacityReduction.status == CapacityReductionStatus.PENDING_SELECTION
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._first(statement)

    async def claim_due(
        self, reduction_id: uuid.UUID, *, now: datetime
    ) -> ChannelCapacityReduction | None:
        """This reduction, locked, if it is still open and its grace has ended.

        `SKIP LOCKED`, so a second worker that reached the same reduction moves
        on rather than waiting to disable what the first already disabled.
        """
        return await self._first(
            self._select()
            .where(
                ChannelCapacityReduction.id == reduction_id,
                ChannelCapacityReduction.status == CapacityReductionStatus.PENDING_SELECTION,
                ChannelCapacityReduction.grace_ends_at <= now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    async def latest(self) -> ChannelCapacityReduction | None:
        return await self._first(
            self._select().order_by(
                ChannelCapacityReduction.created_at.desc(), ChannelCapacityReduction.id.desc()
            )
        )

    async def active_connections(self) -> list[ChannelConnection]:
        """The connections taking a slot, oldest first: active and unreleased."""
        rows = await self.session.scalars(
            select(ChannelConnection)
            .where(
                ChannelConnection.tenant_id == self.tenant_id,
                ChannelConnection.status == ConnectionStatus.ACTIVE,
                ChannelConnection.released_at.is_(None),
            )
            .order_by(ChannelConnection.ownership_started_at, ChannelConnection.id)
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def live_connections(
        self, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, ChannelConnection]:
        """This workspace's unreleased connections among `ids` - never another's."""
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = await self.session.scalars(
            select(ChannelConnection)
            .where(
                ChannelConnection.tenant_id == self.tenant_id,
                ChannelConnection.id.in_(wanted),
                ChannelConnection.released_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        return {row.id: row for row in rows}

    # ------------------------------------------------------------ pre-selection

    async def preselection(self) -> list[ChannelCapacityPreselection]:
        rows = await self.session.scalars(
            select(ChannelCapacityPreselection)
            .where(ChannelCapacityPreselection.tenant_id == self.tenant_id)
            .order_by(ChannelCapacityPreselection.connection_id)
        )
        return list(rows)

    async def replace_preselection(
        self, connection_ids: Iterable[uuid.UUID], *, by: uuid.UUID | None, at: datetime
    ) -> None:
        await self.clear_preselection()
        for connection_id in sorted(set(connection_ids)):
            self.session.add(
                ChannelCapacityPreselection(
                    tenant_id=self.tenant_id,
                    connection_id=connection_id,
                    selected_by=by,
                    selected_at=at,
                )
            )
        await self.session.flush()

    async def clear_preselection(self) -> int:
        result = await self.session.execute(
            delete(ChannelCapacityPreselection).where(
                ChannelCapacityPreselection.tenant_id == self.tenant_id
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


class PlatformCapacityReductionRepository(BaseRepository[ChannelCapacityReduction]):
    """Open reductions across the deployment, for the billing worker and the gauges."""

    model = ChannelCapacityReduction

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    def _open(self) -> ColumnElement[bool]:
        return ChannelCapacityReduction.status == CapacityReductionStatus.PENDING_SELECTION

    async def due(self, *, now: datetime, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(reduction, workspace) for open reductions whose grace has ended, oldest first."""
        rows = await self.session.execute(
            select(ChannelCapacityReduction.id, ChannelCapacityReduction.tenant_id)
            .where(self._open(), ChannelCapacityReduction.grace_ends_at <= now)
            .order_by(ChannelCapacityReduction.grace_ends_at, ChannelCapacityReduction.id)
            .limit(limit)
        )
        return [(row[0], row[1]) for row in rows.all()]

    async def claim_unnotified(self, *, limit: int) -> list[ChannelCapacityReduction]:
        """Open reductions whose owners have not been told, locked, `SKIP LOCKED`."""
        return await self._all(
            self._select()
            .where(self._open(), ChannelCapacityReduction.notified_at.is_(None))
            .order_by(ChannelCapacityReduction.created_at, ChannelCapacityReduction.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def claim_unwarned(
        self, *, now: datetime, ahead: timedelta, limit: int
    ) -> list[ChannelCapacityReduction]:
        """Open reductions within `ahead` of their grace end, not yet warned."""
        return await self._all(
            self._select()
            .where(
                self._open(),
                ChannelCapacityReduction.warned_at.is_(None),
                ChannelCapacityReduction.grace_ends_at <= now + ahead,
            )
            .order_by(ChannelCapacityReduction.grace_ends_at, ChannelCapacityReduction.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def open_count(self) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(ChannelCapacityReduction).where(self._open())
            )
            or 0
        )


__all__ = [
    "ChannelCapacityReductionRepository",
    "PlatformCapacityReductionRepository",
]
