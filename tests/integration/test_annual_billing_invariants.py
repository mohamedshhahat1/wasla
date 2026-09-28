"""The entitlement engine against an independent SQL oracle (ADR-116).

A generated population of workspaces - monthly and yearly prices, every serving
and non-serving status, finite, zero and unlimited allowances, top-ups and
platform grants that are live, expired, not yet started and under refund
review, usage scattered across the year - and a set of moments across each
term. For every workspace, key and moment, `EntitlementService.check` and
`annual_billing_oracle.oracle_entitlement` must agree on the effective limit,
the usage counted and the usage window. The oracle derives the usage cycle from
the billing anchor with PostgreSQL's own month arithmetic, never from the
stored cycle the service reads.

The same population is then swept by the invariant ledger.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    PlanVersion,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.services import billing_calendar
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from tests.integration.annual_billing_oracle import ledger_violations, oracle_entitlement
from tests.integration.plan_catalogue import own_plan

pytestmark = pytest.mark.integration

WORKSPACES = 48
MOMENTS = 5
KEYS = (
    LimitKey.PERIOD_AI_TURNS,
    LimitKey.PERIOD_MESSAGES,
    LimitKey.PERIOD_CAMPAIGN_MESSAGES,
    LimitKey.WHATSAPP_NUMBERS,
    LimitKey.TEAM_MEMBERS,
    LimitKey.STORAGE_BYTES,
)
STATUSES = (
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.CANCELLED,
    SubscriptionStatus.EXPIRED,
    SubscriptionStatus.SUSPENDED,
)
LIMITS = (
    {"period_ai_turns": 500, "period_messages": 2_000, "whatsapp_numbers": 2},
    {"period_ai_turns": 0, "period_campaign_messages": 0, "team_members": 3},
    {"period_messages": 10_000},  # AI turns unlimited
    {"period_ai_turns": 25_000, "storage_bytes": 10**9, "team_members": 10},
)


async def _plans(session: AsyncSession) -> list[tuple[Plan, PlanVersion, PlanPrice, PlanPrice]]:
    await own_plan(session, code="starter", price=Decimal("0.00"), limits={"period_ai_turns": 50})
    catalog = PlanCatalog(session)
    built = []
    for index, limits in enumerate(LIMITS):
        plan = await own_plan(
            session,
            code=f"oracle-{index}-{uuid.uuid4().hex[:6]}",
            price=Decimal("100.00"),
            limits=limits,
        )
        version = await catalog.current_version(plan)
        assert version is not None
        monthly = await catalog.price_for_term(version, interval=BillingInterval.MONTHLY)
        assert monthly is not None
        yearly = PlanPrice(
            plan_version_id=version.id,
            billing_interval=BillingInterval.YEARLY,
            interval_count=1,
            amount=Decimal("1000.00"),
            currency="EGP",
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.add(yearly)
        await session.flush()
        built.append((plan, version, monthly, yearly))
    return built


def _anchor(rng: random.Random) -> datetime:
    day = rng.choice([1, 15, 28, 29, 30, 31])
    month = rng.randint(1, 12)
    for candidate in (day, 30, 29, 28):
        try:
            return datetime(2026, month, candidate, rng.randint(0, 23), 0, tzinfo=UTC)
        except ValueError:
            continue
    raise AssertionError("unreachable")


async def _population(session: AsyncSession, rng: random.Random) -> list[dict[str, Any]]:
    plans = await _plans(session)
    cases: list[dict[str, Any]] = []
    for index in range(WORKSPACES):
        plan, version, monthly, yearly = plans[index % len(plans)]
        price = yearly if index % 2 else monthly
        status = STATUSES[index % len(STATUSES)]
        anchor = _anchor(rng)
        end = billing_calendar.add_interval(anchor, price.billing_interval)
        tenant = Tenant(name=f"Oracle {index}", slug=f"oracle-{uuid.uuid4().hex[:10]}")
        session.add(tenant)
        await session.flush()
        # A stale stored cycle on half the yearly ones: the sweep has not run.
        first_end = billing_calendar.add_interval(anchor, BillingInterval.MONTHLY)
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            plan_price_id=price.id,
            status=status,
            billing_anchor_at=anchor,
            current_period_start=anchor,
            current_period_end=end,
            usage_period_start=anchor,
            usage_period_end=first_end if price is yearly else end,
            ended_at=(
                anchor + timedelta(days=3)
                if status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED)
                else None
            ),
        )
        session.add(subscription)
        span = (end - anchor).total_seconds()
        for _ in range(12):
            at = anchor + timedelta(seconds=rng.uniform(0, span))
            session.add(
                UsageEvent(
                    tenant_id=tenant.id,
                    event_type=rng.choice(
                        [
                            UsageEventType.AI_TURN,
                            UsageEventType.WHATSAPP_MESSAGE_SENT,
                            UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
                            UsageEventType.CAMPAIGN_MESSAGE,
                        ]
                    ),
                    quantity=rng.randint(1, 40),
                    unit=UsageUnit.COUNT,
                    occurred_at=at,
                )
            )
        await session.flush()
        for entitlement in (
            TopupEntitlement.PERIOD_AI_TURNS,
            TopupEntitlement.WHATSAPP_NUMBERS,
            TopupEntitlement.PERIOD_MESSAGES,
        ):
            state = rng.choice(["live", "expired", "future", "review", "cancelled"])
            granted = anchor + timedelta(days=rng.randint(0, 20))
            if state == "future":
                granted = end - timedelta(days=2)
            # A usage grant lasts at most its month; a capacity one may be longer.
            longest = 400 if entitlement is TopupEntitlement.WHATSAPP_NUMBERS else 30
            expires = granted + timedelta(days=rng.randint(5, longest))
            if state == "expired":
                expires = granted + timedelta(days=1)
            session.add(
                TopupPurchase(
                    tenant_id=tenant.id,
                    subscription_id=subscription.id,
                    source=TopupSource.PLATFORM_GRANT,
                    product_name="Platform grant",
                    entitlement_key=entitlement,
                    quantity=rng.randint(1, 900),
                    unit_price=Decimal("0.00"),
                    total_amount=Decimal("0.00"),
                    currency="EGP",
                    billing_period_start=granted - timedelta(days=1),
                    billing_period_end=expires,
                    expires_at=expires,
                    status={
                        "live": TopupStatus.GRANTED,
                        "expired": TopupStatus.EXPIRED,
                        "future": TopupStatus.GRANTED,
                        "review": TopupStatus.REFUND_REVIEW,
                        "cancelled": TopupStatus.CANCELLED,
                    }[state],
                    granted_at=granted if state != "cancelled" else None,
                    reason="Oracle population.",
                )
            )
        await session.flush()
        moments = [anchor + timedelta(seconds=rng.uniform(0, span)) for _ in range(MOMENTS)]
        cases.append({"tenant": tenant, "moments": moments})
    return cases


def _frozen(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


async def test_the_entitlement_engine_agrees_with_the_sql_oracle_monthly_and_yearly(
    db_session: AsyncSession,
) -> None:
    rng = random.Random(116)  # noqa: S311 - a reproducible population, not a secret
    cases = await _population(db_session, rng)
    comparisons = 0
    mismatches: list[str] = []
    for case in cases:
        tenant = case["tenant"]
        for at in case["moments"]:
            moment: datetime = at
            service = EntitlementService(
                db_session,
                tenant_id=tenant.id,
                default_plan_code="starter",
                clock=_frozen(moment),
            )
            for key in KEYS:
                got = await service.check(key, additional=0)
                want = await oracle_entitlement(db_session, tenant.id, key, at=at)
                comparisons += 1
                if got.limit != want.limit:
                    mismatches.append(
                        f"{tenant.slug} {key.value} @ {at}: limit {got.limit} != {want.limit}"
                    )
                if want.window is not None:
                    if (got.period_start, got.period_end) != want.window:
                        mismatches.append(
                            f"{tenant.slug} {key.value} @ {at}: window "
                            f"{(got.period_start, got.period_end)} != {want.window}"
                        )
                    if got.used != want.used:
                        mismatches.append(
                            f"{tenant.slug} {key.value} @ {at}: used {got.used} != {want.used}"
                        )
    print(f"entitlement comparisons: {comparisons}")  # noqa: T201 - recorded as evidence
    assert comparisons == WORKSPACES * MOMENTS * len(KEYS)
    assert mismatches == []
    found = await ledger_violations(db_session, tenant_ids=[case["tenant"].id for case in cases])
    assert found == {}, found
