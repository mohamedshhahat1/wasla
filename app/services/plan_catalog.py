"""Which immutable terms apply: the one place versions and prices are resolved.

Every question with money or a limit behind it is answered from a `PlanVersion`
(what is allowed) and a `PlanPrice` (what is paid, and how often), never from
the `plans` row (BILL-12, ADR-116). This module is how the rest of billing finds
the right ones:

- **What does a new customer get today?** `current_version` - the newest
  version whose `effective_at` has arrived - at one of its *active* prices.
- **What is this subscriber held to?** `pinned_version` and `pinned_price` -
  the terms the subscription points at, which a catalogue edit, a new price
  or a retired one does not move.
- **May a customer buy this price now?** `selectable_price` - the single
  check every checkout, plan change and offer runs.

A plan with no versions at all is one that predates versioning or was written
by hand (a test fixture, an operator's SQL). Its first version is *materialised*
from the plan row the first time anything asks - exactly the snapshot migration
0071 took of every plan that existed when it ran - so there is one code path
for "the terms of a plan" whatever the row's origin. Materialising is
concurrency-safe: two callers racing both insert version 1, the unique
constraint keeps one, and the loser re-reads it. The database publishes a priced
version's first price as it is inserted, so a materialised version is sellable
the moment it exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CustomPlanNotAvailableError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.billing import BillingInterval, Plan, PlanPrice, PlanVersion, Subscription
from app.repositories.billing_repository import (
    PlanPriceRepository,
    PlanRepository,
    PlanVersionRepository,
)
from app.services.entitlement_terms import LEGACY_CHANNEL_TYPES, ordered

logger = get_logger(__name__)

MATERIALISED_REASON = "Initial version, snapshotted from the plan catalogue."
# When a materialised first version takes effect: the beginning. It is not a
# new offer but the terms the plan has always had, so it must be in effect for
# any moment a caller asks about - including a subscription that started, or a
# sweep that runs, before the version row was written. Migration 0071 uses the
# same instant for the versions it backfills.
ORIGINAL_TERMS_EFFECTIVE_AT = datetime(1970, 1, 1, tzinfo=UTC)

# What a customer is told about a price they may not buy. One message for a
# price that does not exist and for another workspace's, so a probe learns
# nothing about which ids are real.
NO_SUCH_PRICE = "No such price."
PRICE_NOT_OFFERED = "That price is no longer offered. Choose one from GET /billing/plans."


@dataclass(frozen=True, slots=True)
class PricedTerms:
    """One plan, the version it grants and the price it is paid at.

    `price` is None exactly when the version is free.
    """

    plan: Plan
    version: PlanVersion
    price: PlanPrice | None


class PlanCatalog:
    """Resolves plan versions and prices for customers and subscribers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._plans = PlanRepository(session)
        self._versions = PlanVersionRepository(session)
        self._prices = PlanPriceRepository(session)

    async def offered(
        self, *, tenant_id: uuid.UUID | None = None
    ) -> list[tuple[Plan, PlanVersion]]:
        """The active catalogue at the terms a new customer would buy.

        Public plans, plus - when `tenant_id` is given - that workspace's own
        custom plans, and never anybody else's (ADR-113).
        """
        listed: list[tuple[Plan, PlanVersion]] = []
        for plan in await self._plans.list_plans(for_tenant=tenant_id):
            version = await self.current_version(plan)
            if version is not None:
                listed.append((plan, version))
        return listed

    async def get_version(self, version_id: object) -> PlanVersion | None:
        if version_id is None:
            return None
        return await self._versions.get_by_id(version_id)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- prices

    async def get_price(self, price_id: object) -> PlanPrice | None:
        if price_id is None:
            return None
        return await self._prices.get_by_id(price_id)  # type: ignore[arg-type]

    async def prices(self, version: PlanVersion, *, active_only: bool = True) -> list[PlanPrice]:
        """A version's prices, shortest term first. Free versions have none."""
        if version.is_free:
            return []
        await self._session.flush()
        return await self._prices.for_version(version.id, active_only=active_only)

    async def price_for_term(
        self,
        version: PlanVersion,
        *,
        interval: BillingInterval,
        interval_count: int = 1,
    ) -> PlanPrice | None:
        """The version's active price for one billing term, if it sells one."""
        if version.is_free:
            return None
        await self._session.flush()
        return await self._prices.active_for_slot(
            version.id,
            interval=interval,
            interval_count=interval_count,
            currency=version.currency,
        )

    async def default_price(self, version: PlanVersion) -> PlanPrice | None:
        """The price a request naming only a plan code buys (ADR-116, compatibility).

        The active monthly price, which is what every plan code meant before
        yearly prices existed; failing that, the active price on the term the
        version was published with; failing that, none - and a caller must
        then name a price explicitly. Never a guess between two terms.
        """
        monthly = await self.price_for_term(version, interval=BillingInterval.MONTHLY)
        if monthly is not None:
            return monthly
        if version.interval is not BillingInterval.MONTHLY:
            return await self.price_for_term(version, interval=version.interval)
        return None

    async def publication_price(self, version: PlanVersion) -> PlanPrice | None:
        """The price a version was published with, retired or not.

        Only for pinning a row written before prices existed: its terms were
        the version's own, and this is the row the database made of them.
        """
        if version.is_free:
            return None
        await self._session.flush()
        for price in await self._prices.for_version(version.id):
            if (
                price.billing_interval is version.interval
                and price.interval_count == 1
                and price.amount == version.price
                and price.currency == version.currency
            ):
                return price
        return None

    async def selectable_price(
        self,
        price_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        at: datetime,
    ) -> PricedTerms:
        """A price a workspace's customer may choose right now, with its terms.

        The single gate on what a new purchase may be priced at. Refused
        (`ValidationError`, 422) when the price:

        - does not exist, or belongs to another workspace's custom plan - one
          message for both, so a probe learns nothing;
        - is retired, or belongs to a version a newer one has replaced - it is
          no longer offered to anybody new;
        - belongs to a plan that is retired, or not yet in effect.

        Scope beyond ownership - whether a public plan may be bought at a
        checkout, or a custom one only through its offer - is the caller's.
        """
        price = await self._prices.get_by_id(price_id)
        if price is None:
            raise ValidationError(NO_SUCH_PRICE)
        version = await self._versions.get_by_id(price.plan_version_id)
        if version is None:  # pragma: no cover - the foreign key forbids it
            raise ValidationError(NO_SUCH_PRICE)
        plan = await self._plans.get_by_id(version.plan_id)
        if plan is None or not plan.available_to(tenant_id):
            raise ValidationError(NO_SUCH_PRICE)
        if not plan.is_active:
            raise ValidationError("No such plan.")
        current = await self.current_version(plan, at=at)
        if not price.is_active or current is None or current.id != version.id:
            raise ValidationError(PRICE_NOT_OFFERED)
        return PricedTerms(plan=plan, version=version, price=price)

    # -------------------------------------------------------------- versions

    async def require_available(
        self,
        target: Plan | PlanVersion,
        *,
        tenant_id: uuid.UUID,
    ) -> Plan:
        """The plan behind `target`, if `tenant_id` may hold it at all (ADR-113).

        The service-side half of the TENANT binding, called by every path that
        points a workspace at a plan: starting, changing, scheduling, granting
        a purchase and adopting a renewal. A trigger enforces the same rule on
        the tables; this one answers first, with an error an operator can read.

        Raises `CustomPlanNotAvailableError`. Tenant-facing callers translate
        it into the ordinary "No such plan." so a workspace probing codes
        learns nothing.
        """
        plan = target if isinstance(target, Plan) else await self._plans.get_by_id(target.plan_id)
        if plan is None:  # pragma: no cover - RESTRICT makes this unreachable
            raise NotFoundError("No such plan.")
        if not plan.available_to(tenant_id):
            logger.warning(
                "billing.custom_plan_scope_refused",
                extra={
                    "event": "billing.custom_plan_scope_refused",
                    "tenant_id": str(tenant_id),
                    "plan": plan.code,
                },
            )
            raise CustomPlanNotAvailableError()
        return plan

    async def current_version(
        self, plan: Plan, *, at: datetime | None = None
    ) -> PlanVersion | None:
        """The terms a customer choosing this plan at `at` would buy.

        None when every version of the plan is scheduled for the future: the
        plan exists but is not yet on sale, and a checkout must say so rather
        than sell next month's terms today.
        """
        moment = at if at is not None else datetime.now(UTC)
        version = await self._versions.effective(plan.id, at=moment)
        if version is not None:
            return version
        if await self._versions.latest(plan.id) is None:
            return await self._materialise(plan)
        return None

    async def pinned_version(
        self,
        subscription: Subscription,
        *,
        at: datetime | None = None,
    ) -> PlanVersion | None:
        """The terms this subscription is held to.

        A subscription written before versioning, or inserted without one, is
        pinned to its plan's current version - and that version's published
        price - the first time it is read. From then on it stays there, which
        is the property that protects a subscriber from the next catalogue edit.
        """
        if subscription.plan_version_id is not None:
            pinned = await self._versions.get_by_id(subscription.plan_version_id)
            if pinned is not None:
                return pinned
        plan = await self._plans.get_by_id(subscription.plan_id)
        if plan is None:  # pragma: no cover - RESTRICT makes this unreachable
            return None
        version = await self.current_version(plan, at=at)
        if version is None:
            # Only future versions exist. The subscription is served on the
            # earliest of them rather than on nothing.
            versions = await self._versions.list_for_plan(plan.id)
            version = versions[-1] if versions else None
        if version is not None and version.plan_id == subscription.plan_id:
            subscription.plan_version_id = version.id
            if subscription.plan_price_id is None and not version.is_free:
                price = await self.publication_price(version) or await self.default_price(version)
                subscription.plan_price_id = price.id if price is not None else None
        return version

    async def pinned_price(
        self,
        subscription: Subscription,
        *,
        version: PlanVersion | None = None,
    ) -> PlanPrice | None:
        """The price this subscription renews at, or None on a free version.

        Pinned like the version, and lazily for the same kind of legacy row:
        a priced subscription that names no price takes its version's
        published one, which is what it was always billed at.
        """
        terms = version if version is not None else await self.pinned_version(subscription)
        if terms is None or terms.is_free:
            return None
        if subscription.plan_price_id is not None:
            pinned = await self._prices.get_by_id(subscription.plan_price_id)
            if pinned is not None and pinned.plan_version_id == terms.id:
                return pinned
        price = await self.publication_price(terms) or await self.default_price(terms)
        if price is not None and subscription.plan_version_id == terms.id:
            subscription.plan_price_id = price.id
        return price

    async def _materialise(self, plan: Plan) -> PlanVersion:
        """Version 1 of a plan that has none, snapshotted from the plan row."""
        # A plan added in this session and not yet flushed has its column
        # defaults unapplied; flushing first makes the snapshot read what the
        # row will actually say.
        await self._session.flush()
        version = PlanVersion(
            plan_id=plan.id,
            version=1,
            name=plan.name,
            price=plan.price,
            currency=plan.currency,
            interval=plan.interval,
            trial_days=plan.trial_days,
            limits=dict(plan.limits or {}),
            # A version is always published stating its channel types (ENT-09);
            # a plan row that never stated any materialises the legacy set,
            # never every channel. A row still naming the retired number key is
            # refused by the version trigger - it must say channel_connections.
            allowed_channel_types=(
                list(plan.allowed_channel_types)
                if plan.allowed_channel_types is not None
                else [channel.value for channel in ordered(LEGACY_CHANNEL_TYPES)]
            ),
            # The terms this plan always had - see ORIGINAL_TERMS_EFFECTIVE_AT.
            effective_at=ORIGINAL_TERMS_EFFECTIVE_AT,
            created_at=datetime.now(UTC),
            created_by=None,
            reason=MATERIALISED_REASON,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(version)
                await self._session.flush()
        except IntegrityError:
            existing = await self._versions.latest(plan.id)
            if existing is None:  # pragma: no cover - the row that just blocked us
                raise
            return existing
        logger.info(
            "billing.plan_version_materialised",
            extra={"event": "billing.plan_version_materialised", "plan": plan.code},
        )
        return version


__all__ = [
    "MATERIALISED_REASON",
    "NO_SUCH_PRICE",
    "ORIGINAL_TERMS_EFFECTIVE_AT",
    "PRICE_NOT_OFFERED",
    "PlanCatalog",
    "PricedTerms",
]
