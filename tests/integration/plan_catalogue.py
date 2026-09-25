"""One way for a billing test to own the plan rows it asserts on.

`plans.code` is unique across the installation - the catalogue is platform-wide
rather than per workspace - and migration `0016` **seeds it**. Every billing
fixture that built its own `pro` row therefore worked perfectly against a
model-built schema, where `plans` starts empty, and failed against the schema a
deployment actually has: 157 unique-constraint violations, plus five assertions
comparing a test's expected limits against the migration's.

The consequence was not the arithmetic. It was that Wasla's billing behaviour
had never once been executed against the plan catalogue a real deployment
carries, because the CI job that builds from migrations ran a hand-listed
selection with no billing file in it (WQ-04).

`own_plan` is the fix, and the shape of it is the argument. It does **not** skip
a row it finds - that is what `test_refund_entitlements._catalogue` did, and
taking whatever limits happen to be there is how a test comes to assert against
values nobody in the test chose. It does not invent a unique code per test
either: the codes are `pro`, `starter`, `business`, and they are the codes the
API is called with (`POST /billing/checkout {"plan_code": "pro"}`), so renaming
them would quietly stop testing the path customers use.

Instead it takes the row - creating it, or making an existing one match what the
caller asked for - so the test's assumptions are the test's, stated at the call
site, whichever way the schema was built. Every test runs inside a transaction
that is rolled back, so a seeded row is never left modified for the next one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import BillingInterval, Plan, PlanVersion
from app.services.plan_catalog import ORIGINAL_TERMS_EFFECTIVE_AT

#: What a plan is when the caller did not say. Matches the model defaults rather
#: than the migration's catalogue, so a fixture that names only a code gets a
#: blank plan rather than inheriting whatever the seed happens to hold.
DEFAULTS: dict[str, Any] = {
    "description": None,
    "price": Decimal("0.00"),
    "currency": "EGP",
    "interval": BillingInterval.MONTHLY,
    "trial_days": 0,
    "limits": {},
    "is_public": True,
    "is_active": True,
    "sort_order": 0,
}


async def own_plan(session: AsyncSession, *, code: str, **fields: Any) -> Plan:
    """The plan with this code, holding exactly what the caller asked for.

    Creates it when the catalogue has none, and overwrites the named fields when
    it does. Fields the caller did not name are reset to `DEFAULTS`, which is the
    part that matters: a fixture asserting on `limits` must not inherit a
    seeded `trial_days` it never asked about and never thought about, because
    that is a second way for the schema build to change what a test means.

    `name` defaults from the code, as every one of these fixtures already did.
    """
    values = {**DEFAULTS, "name": code.title(), **fields}

    existing = (await session.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()
    if existing is None:
        plan = Plan(code=code, **values)
        session.add(plan)
        await session.flush()
        return plan

    for attribute, value in values.items():
        setattr(existing, attribute, value)
    await session.flush()
    await _publish_matching_version(session, existing)
    return existing


async def _publish_matching_version(session: AsyncSession, plan: Plan) -> None:
    """Make the plan's *terms* say what the caller asked for, too.

    Money and limits come from plan versions (BILL-12), and a migration-built
    database already carries a seeded version 1 for `pro`, `starter` and
    `business` with the seed's terms. Rewriting the row alone would leave every
    subscription resolving the seed's limits, so when the latest version does
    not match, a new one is published - effective from the epoch, like every
    first version, so it is current for any `now` a test passes. Versions are
    immutable; this adds a row and changes none.
    """
    latest = (
        await session.execute(
            select(PlanVersion)
            .where(PlanVersion.plan_id == plan.id)
            .order_by(PlanVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return
    same = (
        latest.price == plan.price
        and latest.currency == plan.currency
        and latest.interval == plan.interval
        and latest.trial_days == plan.trial_days
        and dict(latest.limits or {}) == dict(plan.limits or {})
    )
    if same:
        return
    session.add(
        PlanVersion(
            plan_id=plan.id,
            version=latest.version + 1,
            name=plan.name,
            price=plan.price,
            currency=plan.currency,
            interval=plan.interval,
            trial_days=plan.trial_days,
            limits=dict(plan.limits or {}),
            effective_at=ORIGINAL_TERMS_EFFECTIVE_AT,
            created_at=datetime.now(UTC),
            reason="Test fixture terms.",
        )
    )
    await session.flush()


__all__ = ["DEFAULTS", "own_plan"]
