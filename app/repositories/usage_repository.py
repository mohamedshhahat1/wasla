"""Data access for usage events: one writer, and the aggregates read back.

Every read here is an aggregate. Usage rows are written far more often than they
are looked at, and nobody ever wants the rows themselves - they want a total for
a window, or a series to draw. Returning the rows would mean a dashboard pulling
a month of message events across the wire to add them up in Python.

The window is half-open, `[since, until)`. Two adjacent months asked for that way
sum to the same figure as the two months asked for together, which is not true of
a closed upper bound: a row landing exactly on midnight would be counted twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select

from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit, unit_for
from app.repositories.base import BaseRepository, TenantScopedRepository


@dataclass(frozen=True, slots=True)
class UsageTotal:
    """How much of one meter was consumed in a window."""

    event_type: UsageEventType
    unit: UsageUnit
    quantity: int
    events: int


@dataclass(frozen=True, slots=True)
class UsagePoint:
    """One day of one meter, for a chart."""

    day: datetime
    event_type: UsageEventType
    quantity: int


@dataclass(frozen=True, slots=True)
class TenantUsageTotal:
    """One workspace's consumption of one meter, for the platform view."""

    tenant_id: uuid.UUID
    event_type: UsageEventType
    unit: UsageUnit
    quantity: int
    events: int


def _window(
    statement: Select[Any],
    *,
    since: datetime | None,
    until: datetime | None,
) -> Select[Any]:
    """Apply the half-open window, if either end was given."""
    if since is not None:
        statement = statement.where(UsageEvent.occurred_at >= since)
    if until is not None:
        statement = statement.where(UsageEvent.occurred_at < until)
    return statement


class UsageEventRepository(TenantScopedRepository[UsageEvent]):
    """Usage written and read for one workspace."""

    model = UsageEvent

    def _tenant_filter(self) -> ColumnElement[bool]:
        return UsageEvent.tenant_id == self.tenant_id

    def record(
        self,
        *,
        event_type: UsageEventType,
        quantity: int = 1,
        occurred_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> UsageEvent:
        """Stage one metered occurrence.

        Not a coroutine: staging is `session.add`, and nothing is read first.
        Making it one would suggest a round trip that does not happen, and would
        put an `await` on every metered path for no reason.

        The unit is taken from the event type rather than accepted as an
        argument - see `app.db.models.usage`.
        """
        event = UsageEvent(
            tenant_id=self.tenant_id,
            event_type=event_type,
            quantity=quantity,
            unit=unit_for(event_type),
            occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
            meta=meta,
        )
        return self.add(event)

    async def totals(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Iterable[UsageEventType] | None = None,
    ) -> list[UsageTotal]:
        """Sum every meter over a window, in one query."""
        statement = (
            select(
                UsageEvent.event_type,
                UsageEvent.unit,
                func.coalesce(func.sum(UsageEvent.quantity), 0),
                func.count(),
            )
            .where(self._tenant_filter())
            .group_by(UsageEvent.event_type, UsageEvent.unit)
            .order_by(UsageEvent.event_type)
        )
        statement = _window(statement, since=since, until=until)
        selected = list(event_types) if event_types is not None else None
        if selected is not None:
            statement = statement.where(UsageEvent.event_type.in_(selected))

        result = await self.session.execute(statement)
        return [
            UsageTotal(event_type=row[0], unit=row[1], quantity=int(row[2]), events=int(row[3]))
            for row in result.all()
        ]

    async def daily(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Iterable[UsageEventType] | None = None,
    ) -> list[UsagePoint]:
        """One point per day per meter, oldest first.

        Days are UTC. A workspace in another timezone sees a boundary that is
        not its midnight, which is a real limitation and the honest one to have
        until a workspace can tell us what its timezone is: guessing from a
        phone number would silently move every figure.
        """
        day = func.date_trunc("day", UsageEvent.occurred_at).label("day")
        statement = (
            select(day, UsageEvent.event_type, func.coalesce(func.sum(UsageEvent.quantity), 0))
            .where(self._tenant_filter())
            .group_by(day, UsageEvent.event_type)
            .order_by(day, UsageEvent.event_type)
        )
        statement = _window(statement, since=since, until=until)
        selected = list(event_types) if event_types is not None else None
        if selected is not None:
            statement = statement.where(UsageEvent.event_type.in_(selected))

        result = await self.session.execute(statement)
        return [
            UsagePoint(day=row[0], event_type=row[1], quantity=int(row[2])) for row in result.all()
        ]


class PlatformUsageRepository(BaseRepository[UsageEvent]):
    """Usage summed across every workspace, for platform administration.

    Deliberately not a `TenantScopedRepository`: it is the one reader that is
    supposed to see everything, and making that obvious in the type is better
    than a scoped class with an escape hatch. Nothing constructs it except the
    platform layer, whose routes are behind the platform-role dependency.
    """

    model = UsageEvent

    async def totals(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[UsageTotal]:
        """Sum every meter across the platform for a window."""
        statement = (
            select(
                UsageEvent.event_type,
                UsageEvent.unit,
                func.coalesce(func.sum(UsageEvent.quantity), 0),
                func.count(),
            )
            .group_by(UsageEvent.event_type, UsageEvent.unit)
            .order_by(UsageEvent.event_type)
        )
        statement = _window(statement, since=since, until=until)
        result = await self.session.execute(statement)
        return [
            UsageTotal(event_type=row[0], unit=row[1], quantity=int(row[2]), events=int(row[3]))
            for row in result.all()
        ]

    async def by_tenant(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Sequence[UsageEventType] | None = None,
        tenant_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[TenantUsageTotal]:
        """Per-workspace totals, for the table a platform dashboard shows.

        `tenant_ids` narrows the sum to the page of workspaces being displayed,
        so listing twenty of them does not aggregate the whole platform.
        """
        statement = (
            select(
                UsageEvent.tenant_id,
                UsageEvent.event_type,
                UsageEvent.unit,
                func.coalesce(func.sum(UsageEvent.quantity), 0),
                func.count(),
            )
            .group_by(UsageEvent.tenant_id, UsageEvent.event_type, UsageEvent.unit)
            .order_by(UsageEvent.tenant_id, UsageEvent.event_type)
        )
        statement = _window(statement, since=since, until=until)
        if event_types is not None:
            statement = statement.where(UsageEvent.event_type.in_(list(event_types)))
        if tenant_ids is not None:
            statement = statement.where(UsageEvent.tenant_id.in_(list(tenant_ids)))

        result = await self.session.execute(statement)
        return [
            TenantUsageTotal(
                tenant_id=row[0],
                event_type=row[1],
                unit=row[2],
                quantity=int(row[3]),
                events=int(row[4]),
            )
            for row in result.all()
        ]
