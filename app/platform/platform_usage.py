"""Usage summed across every workspace.

A thin composition over `PlatformUsageRepository`, which is the one usage reader
that is not tenant-scoped. It lives here rather than beside the tenant service so
that the cross-tenant read is where a reviewer would look for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.usage_repository import (
    PlatformUsageRepository,
    TenantUsageTotal,
    UsageTotal,
)
from app.services.usage_service import (
    UsageSummary,
    UsageWindow,
    resolve_window,
    summarise,
)


def _as_total(row: TenantUsageTotal) -> UsageTotal:
    """Drop the tenant id, which is the grouping key rather than part of the figure."""
    return UsageTotal(
        event_type=row.event_type,
        unit=row.unit,
        quantity=row.quantity,
        events=row.events,
    )


class PlatformUsageService:
    """Reads usage across the platform."""

    def __init__(self, session: AsyncSession) -> None:
        self._usage = PlatformUsageRepository(session)

    async def totals(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageSummary:
        """Every meter, summed over every workspace."""
        window = resolve_window(since=since, until=until)
        totals = await self._usage.totals(since=window.since, until=window.until)
        return summarise(totals, window=window)

    async def by_tenant(
        self,
        tenant_ids: Sequence[uuid.UUID],
        *,
        window: UsageWindow,
    ) -> dict[uuid.UUID, UsageSummary]:
        """Per-workspace summaries for the page being displayed.

        Narrowed to the ids on screen rather than aggregating the whole platform
        and discarding most of it: a dashboard shows twenty workspaces at a
        time, and that difference is the entire cost of the query.

        A workspace that consumed nothing is absent from the query's result and
        present here with an empty summary, so a caller never has to tell "no
        rows" apart from "no usage".
        """
        if not tenant_ids:
            return {}

        rows = await self._usage.by_tenant(
            since=window.since,
            until=window.until,
            tenant_ids=list(tenant_ids),
        )
        grouped: dict[uuid.UUID, list[UsageTotal]] = {}
        for row in rows:
            grouped.setdefault(row.tenant_id, []).append(_as_total(row))

        return {
            tenant_id: summarise(grouped.get(tenant_id, []), window=window)
            for tenant_id in tenant_ids
        }
