"""The capacity-reduction queue across workspaces (PLAT-G2, ADR-132).

Reads only. Staff see every workspace that is - or was - coming down to a
smaller channel capacity, when each grace ends and what the automatic
fallback would do, so support can reach a customer before it does. Nothing
here resolves, extends, shortens or cancels a reduction: whether staff may is
a product decision this stage leaves open.

Each figure that describes a workspace *now* - its active connections, the
fallback preview, the owner's pre-selection - is read through the same
`ChannelCapacityReductions` the owner's capacity page uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.db.models.channel import ChannelConnection, ConnectionStatus
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.tenant import Tenant
from app.schemas.channel_capacity import FallbackPreviewRead
from app.schemas.platform_entitlements import (
    PlatformCapacityReductionDetail,
    PlatformCapacityReductionRead,
)
from app.services.channel_capacity_view import capacity_reductions


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


class CapacityReductionQueue:
    """Every workspace's capacity reductions, for platform staff."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def list_reductions(
        self,
        *,
        statuses: Sequence[CapacityReductionStatus] | None,
        causes: Sequence[CapacityReductionCause] | None,
        tenant_id: uuid.UUID | None,
        grace_ends_before: datetime | None,
        grace_ends_after: datetime | None,
        limit: int,
        offset: int,
    ) -> Page[PlatformCapacityReductionRead]:
        """Across workspaces, the grace ending soonest first, then by id."""
        statement = self._rows()
        if statuses:
            statement = statement.where(ChannelCapacityReduction.status.in_(statuses))
        if causes:
            statement = statement.where(ChannelCapacityReduction.cause.in_(causes))
        if tenant_id is not None:
            statement = statement.where(ChannelCapacityReduction.tenant_id == tenant_id)
        if grace_ends_before is not None:
            statement = statement.where(ChannelCapacityReduction.grace_ends_at < grace_ends_before)
        if grace_ends_after is not None:
            statement = statement.where(ChannelCapacityReduction.grace_ends_at >= grace_ends_after)
        ordered = statement.order_by(
            ChannelCapacityReduction.grace_ends_at.asc().nulls_last(),
            ChannelCapacityReduction.id,
        )
        return await self._page(statement, ordered, limit=limit, offset=offset)

    async def history(
        self, tenant_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PlatformCapacityReductionRead]:
        """One workspace's reductions, newest first. 404 for no such workspace."""
        if await self._session.get(Tenant, tenant_id) is None:
            raise NotFoundError("No such workspace.")
        statement = self._rows().where(ChannelCapacityReduction.tenant_id == tenant_id)
        ordered = statement.order_by(
            ChannelCapacityReduction.effective_at.desc(),
            ChannelCapacityReduction.created_at.desc(),
            ChannelCapacityReduction.id.desc(),
        )
        return await self._page(statement, ordered, limit=limit, offset=offset)

    async def get(self, reduction_id: uuid.UUID) -> PlatformCapacityReductionDetail:
        """One reduction, with the workspace's fallback preview while it is open."""
        found = (
            await self._session.execute(
                self._rows().where(ChannelCapacityReduction.id == reduction_id)
            )
        ).first()
        if found is None:
            raise NotFoundError("No such capacity reduction.")
        reduction, tenant_name = found[0], found[1]
        active = await self._active_counts([reduction.tenant_id])
        row = await self._read(reduction, tenant_name, active=active)
        preview: FallbackPreviewRead | None = None
        preselected: list[uuid.UUID] = []
        if reduction.is_open:
            lifecycle = capacity_reductions(
                self._session, reduction.tenant_id, self._settings, datetime.now(UTC)
            )
            choice = await lifecycle.fallback_preview()
            if choice is not None:
                preview = FallbackPreviewRead(
                    keep=[connection.id for connection in choice.keep],
                    disable=[connection.id for connection in choice.disable],
                )
            preselected = await lifecycle.preselected()
        return PlatformCapacityReductionDetail(
            **row.model_dump(),
            kept_connection_ids=list(reduction.kept_connection_ids or []),
            disabled_connection_ids=list(reduction.disabled_connection_ids or []),
            preselected=preselected,
            automatic_fallback=preview,
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _rows() -> Select[tuple[ChannelCapacityReduction, str]]:
        return select(ChannelCapacityReduction, Tenant.name).join(
            Tenant, Tenant.id == ChannelCapacityReduction.tenant_id
        )

    async def _page(
        self,
        statement: Select[tuple[ChannelCapacityReduction, str]],
        ordered: Select[tuple[ChannelCapacityReduction, str]],
        *,
        limit: int,
        offset: int,
    ) -> Page[PlatformCapacityReductionRead]:
        total = int(
            await self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )
        rows = (await self._session.execute(ordered.limit(limit).offset(offset))).all()
        active = await self._active_counts({row[0].tenant_id for row in rows})
        return Page(
            items=[await self._read(row[0], row[1], active=active) for row in rows], total=total
        )

    async def _active_counts(
        self, tenant_ids: Sequence[uuid.UUID] | set[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Connections taking a slot now, per workspace: active and unreleased."""
        if not tenant_ids:
            return {}
        rows = await self._session.execute(
            select(ChannelConnection.tenant_id, func.count())
            .where(
                ChannelConnection.tenant_id.in_(list(tenant_ids)),
                ChannelConnection.status == ConnectionStatus.ACTIVE,
                ChannelConnection.released_at.is_(None),
            )
            .group_by(ChannelConnection.tenant_id)
        )
        return {row[0]: int(row[1]) for row in rows}

    async def _read(
        self,
        reduction: ChannelCapacityReduction,
        tenant_name: str,
        *,
        active: dict[uuid.UUID, int],
    ) -> PlatformCapacityReductionRead:
        if reduction.is_open:
            choice = await capacity_reductions(
                self._session, reduction.tenant_id, self._settings, datetime.now(UTC)
            ).fallback_preview()
            would_disable = len(choice.disable) if choice is not None else 0
        else:
            would_disable = len(reduction.disabled_connection_ids or [])
        return PlatformCapacityReductionRead(
            id=reduction.id,
            tenant_id=reduction.tenant_id,
            tenant_name=tenant_name,
            cause=reduction.cause,
            status=reduction.status,
            effective_at=reduction.effective_at,
            grace_ends_at=reduction.grace_ends_at,
            target_general_limit=reduction.target_general,
            target_typed_slots=dict(reduction.target_typed),
            target_allowed_channel_types=list(reduction.target_allowed_types),
            active_now=active.get(reduction.tenant_id, 0),
            would_be_disabled_count=would_disable,
            notified_at=reduction.notified_at,
            warned_at=reduction.warned_at,
            resolved_at=reduction.resolved_at,
            topup_purchase_id=reduction.topup_purchase_id,
            revision=reduction.revision,
        )


__all__ = ["CapacityReductionQueue", "Page"]
