"""Which immutable terms apply: the one place plan versions are resolved.

Every question with money or a limit behind it is answered from a
`PlanVersion`, never from the `plans` row (BILL-12). This module is how the rest
of billing finds the right one:

- **What does a new customer get today?** `current_version` - the newest
  version whose `effective_at` has arrived.
- **What is this subscriber held to?** `pinned_version` - the version the
  subscription points at, which a catalogue edit does not move.

A plan with no versions at all is one that predates versioning or was written
by hand (a test fixture, an operator's SQL). Its first version is *materialised*
from the plan row the first time anything asks - exactly the snapshot migration
0071 took of every plan that existed when it ran - so there is one code path
for "the terms of a plan" whatever the row's origin. Materialising is
concurrency-safe: two callers racing both insert version 1, the unique
constraint keeps one, and the loser re-reads it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CustomPlanNotAvailableError, NotFoundError
from app.core.logging import get_logger
from app.db.models.billing import Plan, PlanVersion, Subscription
from app.repositories.billing_repository import PlanRepository, PlanVersionRepository

logger = get_logger(__name__)

MATERIALISED_REASON = "Initial version, snapshotted from the plan catalogue."
# When a materialised first version takes effect: the beginning. It is not a
# new offer but the terms the plan has always had, so it must be in effect for
# any moment a caller asks about - including a subscription that started, or a
# sweep that runs, before the version row was written. Migration 0071 uses the
# same instant for the versions it backfills.
ORIGINAL_TERMS_EFFECTIVE_AT = datetime(1970, 1, 1, tzinfo=UTC)


class PlanCatalog:
    """Resolves plan versions for customers and subscribers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._plans = PlanRepository(session)
        self._versions = PlanVersionRepository(session)

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
        pinned to its plan's current version the first time it is read - and
        from then on it stays there, which is the property that protects a
        subscriber from the next catalogue edit.
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
        return version

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


__all__ = ["MATERIALISED_REASON", "ORIGINAL_TERMS_EFFECTIVE_AT", "PlanCatalog"]
