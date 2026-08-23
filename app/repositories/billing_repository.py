"""Data access for plans and subscriptions.

The split mirrors usage. A workspace reads exactly one subscription - its own -
through a tenant-scoped repository that cannot express any other query; the
platform reads across every workspace through a separate class that is obviously
not scoped. Making the cross-tenant reader a different type is what keeps the
exception visible.

Plans are not tenant-owned at all. They are the platform's catalogue, read by
everyone, so their repository takes no tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select

from app.db.models.billing import Plan, Subscription, SubscriptionStatus
from app.repositories.base import BaseRepository, TenantScopedRepository


@dataclass(frozen=True, slots=True)
class SubscriptionCount:
    """How many workspaces are in one subscription state."""

    status: SubscriptionStatus
    count: int


class PlanRepository(BaseRepository[Plan]):
    """The platform's catalogue.

    Not tenant-scoped by nature: every workspace reads the same rows, and a plan
    belongs to nobody in particular.
    """

    model = Plan

    async def get_by_id(self, plan_id: uuid.UUID) -> Plan | None:
        return await self._first(self._select().where(Plan.id == plan_id))

    async def get_by_code(self, code: str) -> Plan | None:
        return await self._first(self._select().where(Plan.code == code.strip().lower()))

    async def list_plans(self, *, public_only: bool = True, active_only: bool = True) -> list[Plan]:
        """The catalogue, in the order a pricing page shows it.

        `public_only` excludes plans written for one customer. `active_only`
        excludes retired ones - which are kept rather than deleted, because
        subscriptions still point at them and their history has to keep meaning
        what it meant.
        """
        statement = self._select()
        if public_only:
            statement = statement.where(Plan.is_public.is_(True))
        if active_only:
            statement = statement.where(Plan.is_active.is_(True))
        return await self._all(statement.order_by(Plan.sort_order, Plan.price, Plan.name))


class SubscriptionRepository(TenantScopedRepository[Subscription]):
    """One workspace's subscription.

    Every read is scoped, including the one that fetches "the" subscription:
    the unique index guarantees there is at most one, and the tenant filter
    guarantees it is this workspace's.
    """

    model = Subscription

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Subscription.tenant_id == self.tenant_id

    async def get(self) -> Subscription | None:
        return await self._first(self._select())

    def create(
        self,
        *,
        plan_id: uuid.UUID,
        status: SubscriptionStatus,
        current_period_start: datetime,
        current_period_end: datetime,
        trial_ends_at: datetime | None = None,
    ) -> Subscription:
        return self.add(
            Subscription(
                tenant_id=self.tenant_id,
                plan_id=plan_id,
                status=status,
                current_period_start=current_period_start,
                current_period_end=current_period_end,
                trial_ends_at=trial_ends_at,
            )
        )


class PlatformSubscriptionRepository(BaseRepository[Subscription]):
    """Subscriptions across every workspace, for platform reporting and sweeps.

    Deliberately not scoped, and deliberately a separate class. Nothing
    constructs it except the platform layer and the billing worker.
    """

    model = Subscription

    async def counts(self) -> list[SubscriptionCount]:
        result = await self.session.execute(
            select(Subscription.status, func.count())
            .group_by(Subscription.status)
            .order_by(Subscription.status)
        )
        return [SubscriptionCount(status=row[0], count=int(row[1])) for row in result.all()]

    async def due(self, *, now: datetime, limit: int = 100) -> Sequence[Subscription]:
        """Subscriptions whose period has run out and needs attention.

        Terminal ones are excluded: a cancelled subscription's period ending is
        not an event, it is the past. The caller decides what "attention" means
        - ending a trial, rolling a period over - because those are different
        decisions and both are reached by the same query.
        """
        statement = (
            self._select()
            .where(Subscription.current_period_end <= now)
            .where(
                Subscription.status.in_(
                    [
                        SubscriptionStatus.TRIALING,
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.PAST_DUE,
                    ]
                )
            )
            .order_by(Subscription.current_period_end)
            .limit(limit)
        )
        return await self._all(statement)
