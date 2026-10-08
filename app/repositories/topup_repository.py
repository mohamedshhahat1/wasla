"""Data access for top-up products and the purchases workspaces hold (ADR-113).

Split like every other billing table. The product catalogue belongs to the
platform and is read unscoped, with visibility decided per workspace in the
query. A workspace's purchases are read through a tenant-scoped repository that
cannot express another workspace's rows; the platform's cross-tenant reader is a
separate class, so the exception stays visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, delete, func, or_, select

from app.db.models.billing import Plan
from app.db.models.channel import Channel
from app.db.models.topup import (
    ACTIVE_TOPUP_STATUSES,
    CHANNEL_SLOT_ENTITLEMENTS,
    TopupEntitlement,
    TopupProduct,
    TopupProductPlan,
    TopupPurchase,
    TopupScope,
    TopupSource,
    TopupStatus,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


def _active_at(moment: datetime) -> ColumnElement[bool]:
    """The one predicate for "this top-up counts right now".

    Granted (or under refund review, which still counts until an operator
    decides), and inside `[granted_at, expires_at)`. The clock decides expiry,
    not the sweep that later records it, so an allowance never outlives its
    period because a worker was down.
    """
    return and_(
        TopupPurchase.status.in_([status.value for status in ACTIVE_TOPUP_STATUSES]),
        TopupPurchase.granted_at.is_not(None),
        TopupPurchase.granted_at <= moment,
        TopupPurchase.expires_at > moment,
    )


def _offered_to(plan_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Offered to every plan - no eligibility rows - or to this one (ENT-13)."""
    listed = select(TopupProductPlan.plan_id).where(
        TopupProductPlan.topup_product_id == TopupProduct.id
    )
    unrestricted = ~listed.exists()
    if plan_id is None:
        return unrestricted
    return or_(unrestricted, listed.where(TopupProductPlan.plan_id == plan_id).exists())


@dataclass(frozen=True, slots=True)
class ActiveTotal:
    """How much one entitlement is raised by one source, right now.

    `channel_type` is set only for a typed channel slot (ENT-11); every other
    total, a general channel slot included, has none.
    """

    entitlement: TopupEntitlement
    source: TopupSource
    quantity: int
    channel_type: Channel | None = None


class TopupProductRepository(BaseRepository[TopupProduct]):
    """The platform's top-up catalogue."""

    model = TopupProduct

    async def get_by_id(self, product_id: uuid.UUID) -> TopupProduct | None:
        return await self._first(self._select().where(TopupProduct.id == product_id))

    async def get_by_code(self, code: str) -> TopupProduct | None:
        return await self._first(self._select().where(TopupProduct.code == code.strip().lower()))

    async def visible_to(
        self,
        tenant_id: uuid.UUID,
        *,
        entitlement: TopupEntitlement | None = None,
        plan_id: uuid.UUID | None = None,
        channel_types: Collection[Channel] | None = None,
    ) -> list[TopupProduct]:
        """What one workspace's customer may see and buy.

        Active and public, and either global or written for this workspace.
        Another workspace's product is excluded in the query itself, so no
        response built from this can carry one.

        With `channel_types` - the plan in force's, which `TopupService` always
        passes - also offered to that plan (ENT-13: no eligibility rows, or a
        row for `plan_id`) and, if typed, for a channel the plan allows
        (ENT-12): a slot the workspace could never use is not on its list.
        """
        statement = (
            self._select()
            .where(TopupProduct.is_active.is_(True))
            .where(TopupProduct.is_public.is_(True))
            .where(
                or_(
                    TopupProduct.scope == TopupScope.GLOBAL,
                    TopupProduct.tenant_id == tenant_id,
                )
            )
        )
        if entitlement is not None:
            statement = statement.where(TopupProduct.entitlement_key == entitlement)
        if channel_types is not None:
            statement = statement.where(_offered_to(plan_id)).where(
                or_(
                    TopupProduct.channel_type.is_(None),
                    TopupProduct.channel_type.in_(list(channel_types)),
                )
            )
        return await self._all(
            statement.order_by(TopupProduct.entitlement_key, TopupProduct.price, TopupProduct.code)
        )

    async def offered_to(self, product_id: uuid.UUID, plan_id: uuid.UUID | None) -> bool:
        """Whether the product is offered to workspaces on `plan_id` (ENT-13)."""
        eligible = await self.eligible_plan_ids(product_id)
        return not eligible or plan_id in eligible

    async def eligible_plan_ids(self, product_id: uuid.UUID) -> set[uuid.UUID]:
        """The plans a product is offered to; empty means every plan."""
        rows = await self.session.scalars(
            select(TopupProductPlan.plan_id).where(TopupProductPlan.topup_product_id == product_id)
        )
        return set(rows)

    async def eligible_plan_codes(self, product_id: uuid.UUID) -> list[str]:
        """The codes of the plans a product is offered to, sorted; empty means every plan."""
        rows = await self.session.scalars(
            select(Plan.code)
            .join(TopupProductPlan, TopupProductPlan.plan_id == Plan.id)
            .where(TopupProductPlan.topup_product_id == product_id)
            .order_by(Plan.code)
        )
        return list(rows)

    async def set_eligible_plans(
        self, product_id: uuid.UUID, plan_ids: Iterable[uuid.UUID], *, now: datetime
    ) -> None:
        """Offer the product to exactly these plans; none means every plan (ENT-13)."""
        await self.session.execute(
            delete(TopupProductPlan).where(TopupProductPlan.topup_product_id == product_id)
        )
        for plan_id in sorted(set(plan_ids)):
            self.session.add(
                TopupProductPlan(topup_product_id=product_id, plan_id=plan_id, created_at=now)
            )
        await self.session.flush()

    async def purchase_count(self, product_id: uuid.UUID) -> int:
        """How many purchases name this product - the financial references."""
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(TopupPurchase)
                .where(TopupPurchase.topup_product_id == product_id)
            )
            or 0
        )


class TopupPurchaseRepository(TenantScopedRepository[TopupPurchase]):
    """One workspace's top-ups, bought and granted."""

    model = TopupPurchase

    def _tenant_filter(self) -> ColumnElement[bool]:
        return TopupPurchase.tenant_id == self.tenant_id

    async def get_by_id(self, purchase_id: uuid.UUID) -> TopupPurchase | None:
        return await self._first(self._select().where(TopupPurchase.id == purchase_id))

    async def get_by_idempotency_key(self, key: str) -> TopupPurchase | None:
        return await self._first(self._select().where(TopupPurchase.idempotency_key == key))

    async def lock_for_invoice(self, invoice_id: uuid.UUID) -> TopupPurchase | None:
        """The purchase an invoice pays for, locked for the settlement deciding it.

        `uq_topup_purchases_invoice_id` makes there be at most one. The lock
        serialises two settlements of the same invoice, so the grant happens
        once however the money is reported.
        """
        purchase = await self._first(
            self._select().where(TopupPurchase.invoice_id == invoice_id).with_for_update()
        )
        if purchase is not None:
            # Flushed first: a refresh reloads from the row and would drop a
            # change this transaction made and has not yet written.
            await self.session.flush()
            await self.session.refresh(purchase)
        return purchase

    async def active_totals(self, *, at: datetime) -> list[ActiveTotal]:
        """What every live top-up adds, per entitlement, source and channel type, at `at`."""
        rows = await self.session.execute(
            select(
                TopupPurchase.entitlement_key,
                TopupPurchase.source,
                TopupPurchase.channel_type,
                func.coalesce(func.sum(TopupPurchase.quantity), 0),
            )
            .where(self._tenant_filter())
            .where(_active_at(at))
            .group_by(
                TopupPurchase.entitlement_key, TopupPurchase.source, TopupPurchase.channel_type
            )
        )
        return [
            ActiveTotal(
                entitlement=row[0], source=row[1], channel_type=row[2], quantity=int(row[3])
            )
            for row in rows.all()
        ]

    async def active(self, *, at: datetime) -> list[TopupPurchase]:
        """Every top-up live at `at`, soonest to expire first."""
        return await self._all(
            self._select()
            .where(_active_at(at))
            .order_by(TopupPurchase.expires_at, TopupPurchase.created_at)
        )

    async def history(self, *, limit: int, offset: int) -> tuple[list[TopupPurchase], int]:
        """Newest first, with the total for paging."""
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(TopupPurchase).where(self._tenant_filter())
            )
            or 0
        )
        rows = await self._all(
            self._select()
            .order_by(TopupPurchase.created_at.desc(), TopupPurchase.id)
            .limit(limit)
            .offset(offset)
        )
        return rows, total


class PlatformTopupPurchaseRepository(BaseRepository[TopupPurchase]):
    """Top-ups across every workspace, for the platform and the expiry sweep.

    Deliberately unscoped and deliberately its own class, like every other
    platform reader. Nothing a workspace can reach constructs it.
    """

    model = TopupPurchase

    async def get_by_id(self, purchase_id: uuid.UUID) -> TopupPurchase | None:
        return await self._first(self._select().where(TopupPurchase.id == purchase_id))

    async def lock(self, purchase_id: uuid.UUID) -> TopupPurchase | None:
        purchase = await self._first(
            self._select().where(TopupPurchase.id == purchase_id).with_for_update()
        )
        if purchase is not None:
            # Flushed first: a refresh reloads from the row and would drop a
            # change this transaction made and has not yet written.
            await self.session.flush()
            await self.session.refresh(purchase)
        return purchase

    async def claim_expired(self, *, now: datetime, limit: int) -> list[TopupPurchase]:
        """Top-ups past their expiry whose end is not recorded yet, each to one worker.

        Granted ones, which the sweep moves to expired. And channel slots still
        under refund review (ENT-15): they stopped counting at `expires_at` like
        any other, so their workspace's capacity fell then, but they stay in
        review for an operator's decision - only their `ended_at` is recorded,
        once, which is what keeps them from being claimed again.

        Recording expiry is bookkeeping - the limit arithmetic already ignores
        them - so `SKIP LOCKED` rather than waiting: a row somebody else holds
        is somebody else's to record.
        """
        return await self._all(
            self._select()
            .where(
                or_(
                    TopupPurchase.status == TopupStatus.GRANTED,
                    and_(
                        TopupPurchase.status == TopupStatus.REFUND_REVIEW,
                        TopupPurchase.entitlement_key.in_(CHANNEL_SLOT_ENTITLEMENTS),
                        TopupPurchase.ended_at.is_(None),
                    ),
                )
            )
            .where(TopupPurchase.expires_at <= now)
            .order_by(TopupPurchase.expires_at, TopupPurchase.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def stuck(self, *, paid_before: datetime) -> dict[TopupEntitlement, int]:
        """Top-ups paid for and still not granted since before `paid_before`.

        Each is an open incident already; counted per entitlement so an alert
        can see one has been sitting unresolved.
        """
        rows = await self.session.execute(
            select(TopupPurchase.entitlement_key, func.count())
            .where(TopupPurchase.status == TopupStatus.PAID)
            .where(TopupPurchase.paid_at < paid_before)
            .group_by(TopupPurchase.entitlement_key)
        )
        return {row[0]: int(row[1]) for row in rows.all()}
