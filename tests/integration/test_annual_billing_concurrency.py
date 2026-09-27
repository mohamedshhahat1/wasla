# ruff: noqa: F811 - the sweep-concurrency harness fixtures are imported by name.
"""Annual renewals and monthly usage cycles under concurrency (ADR-116).

Real PostgreSQL, real commits, ten workers at once - a claim is a row lock, and
coroutines sharing one transaction could not race for it. Paymob is faked at
the socket and counts every request, so "one charge" is a count of requests the
adapter actually sent, not of rows written afterwards.

1. **Ten renewal workers** at an annual boundary: one renewal invoice, one MOTO
   request for the full year, one settlement, one twelve-month advance.
2. **Ten usage workers** mid-year, months behind: one move to the right cycle,
   no invoice, no provider request.
3. **Renewal racing usage roll-over** at the annual boundary with a stale
   cycle: one annual invoice, the next year's term, and its first month - no
   cycle lost, none opened twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.audit import AuditLog
from app.db.models.billing import BillingInterval, Plan, PlanPrice, Subscription, SubscriptionStatus
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment
from app.db.models.payment_method import PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services import billing_calendar
from app.services.checkout_service import APPLIED, CheckoutService
from app.services.plan_catalog import PlanCatalog
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.billing_fixtures import add_owner, erase_ledger
from tests.fakes import as_database
from tests.integration.test_billing_remediation_journeys import Paymob, _callback
from tests.integration.test_billing_sweep_concurrency import (  # noqa: F401
    PooledHandle,
    _settings,
    committing,
)
from tests.payment_tokens import saved_card

pytestmark = pytest.mark.integration

WORKERS = 10
YEARLY = Decimal("4990.00")
# Clearly synthetic, and in the past so every clock in the run is after it.
START = datetime(2025, 3, 31, 8, 0, tzinfo=UTC)


@dataclass
class World:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    plan_id: uuid.UUID
    subscription_id: uuid.UUID
    price: PlanPrice


@pytest_asyncio.fixture
async def world(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[World]:
    """A workspace on a yearly price, its year starting at START."""
    code = f"race-{uuid.uuid4().hex[:8]}"
    async with committing() as session:
        plan = Plan(
            code=code,
            name="Race",
            price=Decimal("499.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 3, "period_ai_turns": 1_000},
        )
        session.add(plan)
        await session.flush()
        catalog = PlanCatalog(session)
        version = await catalog.current_version(plan)
        assert version is not None
        yearly = PlanPrice(
            plan_version_id=version.id,
            billing_interval=BillingInterval.YEARLY,
            interval_count=1,
            amount=YEARLY,
            currency="EGP",
            created_at=START - timedelta(days=1),
            reason="Synthetic race price.",
        )
        session.add(yearly)
        tenant = Tenant(name="Race Co", slug=f"race-{uuid.uuid4().hex[:10]}")
        session.add(tenant)
        await session.flush()
        owner = await add_owner(session, tenant)
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            plan_price_id=yearly.id,
            status=SubscriptionStatus.ACTIVE,
            billing_anchor_at=START,
            current_period_start=START,
            current_period_end=START.replace(year=START.year + 1),
            usage_period_start=START,
            usage_period_end=datetime(2025, 4, 30, 8, 0, tzinfo=UTC),
        )
        session.add(subscription)
        session.add(
            saved_card(
                tenant_id=tenant.id,
                provider="paymob",
                token=f"tok-{uuid.uuid4().hex}",
                provider_token_id="1",
                masked_pan="xxxx-2346",
                brand="MasterCard",
                status=PaymentMethodStatus.ACTIVE,
                is_default=True,
            )
        )
        await session.commit()
        built = World(tenant.id, owner.id, plan.id, subscription.id, yearly)
    try:
        yield built
    finally:
        async with committing() as session:
            await session.execute(delete(AuditLog).where(AuditLog.tenant_id == built.tenant_id))
            await erase_ledger(session, [built.tenant_id])
            await session.execute(
                delete(Subscription).where(Subscription.id == built.subscription_id)
            )
            await session.execute(delete(Tenant).where(Tenant.id == built.tenant_id))
            await session.execute(delete(User).where(User.id == built.owner_id))
            await session.execute(delete(Plan).where(Plan.id == built.plan_id))
            await session.commit()


def _workers(
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    paymob: Paymob,
) -> list[BillingWorker]:
    provider = paymob.provider()
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)
    return [
        BillingWorker(database=as_database(PooledHandle(maker)), settings=_settings())
        for _ in range(WORKERS)
    ]


async def _subscription(maker: async_sessionmaker[AsyncSession], world: World) -> Subscription:
    async with maker() as session:
        row = await session.get(Subscription, world.subscription_id)
        assert row is not None
        return row


async def _invoices(maker: async_sessionmaker[AsyncSession], world: World) -> list[Invoice]:
    async with maker() as session:
        return list(
            (
                await session.scalars(select(Invoice).where(Invoice.tenant_id == world.tenant_id))
            ).all()
        )


async def test_ten_annual_renewal_workers_charge_the_year_once(
    committing: async_sessionmaker[AsyncSession],
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paymob = Paymob()
    boundary = START.replace(year=START.year + 1)
    moment = boundary + timedelta(minutes=5)
    await asyncio.gather(
        *(worker.run_once(now=moment) for worker in _workers(committing, monkeypatch, paymob))
    )
    renewals = [
        i for i in await _invoices(committing, world) if i.purpose is InvoicePurpose.RENEWAL
    ]
    assert len(renewals) == 1, "one renewal invoice"
    assert (renewals[0].amount_due, renewals[0].billing_interval) == (
        YEARLY,
        BillingInterval.YEARLY,
    )
    assert len(paymob.pays()) == 1, "one MOTO request"
    assert paymob.intentions()[-1]["body"]["amount"] == int(YEARLY * 100)
    subscription = await _subscription(committing, world)
    assert (subscription.current_period_start, subscription.current_period_end) == (
        boundary,
        boundary.replace(year=boundary.year + 1),
    ), "one twelve-month advance"
    assert subscription.usage_period_end == datetime(2026, 4, 30, 8, 0, tzinfo=UTC)

    # Ten deliveries of the one successful callback settle it once.
    async with committing() as session:
        charge = await session.scalar(
            select(Payment)
            .where(Payment.tenant_id == world.tenant_id)
            .where(Payment.is_automatic.is_(True))
        )
        assert charge is not None
    signed = _callback(charge, transaction=990_000_001)

    async def deliver() -> str:
        async with committing() as session:
            provider = paymob.provider()
            event = provider.verify_callback(payload=signed[0], signature=signed[1])
            outcome = await CheckoutService(
                session, tenant_id=world.tenant_id, provider=provider, default_plan_code="starter"
            ).apply(event, now=moment)
            await session.commit()
            return outcome

    outcomes = await asyncio.gather(*(deliver() for _ in range(WORKERS)), return_exceptions=True)
    assert sum(1 for outcome in outcomes if outcome == APPLIED) == 1, outcomes
    renewal = [i for i in await _invoices(committing, world) if i.purpose is InvoicePurpose.RENEWAL]
    assert renewal[0].status is InvoiceStatus.PAID and renewal[0].amount_paid == YEARLY
    again = await _subscription(committing, world)
    assert again.current_period_end == subscription.current_period_end, "advanced once"


async def test_ten_usage_workers_open_the_right_cycle_once_and_charge_nothing(
    committing: async_sessionmaker[AsyncSession],
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paymob = Paymob()
    moved: list[Any] = []
    original = billing_calendar.current_usage_period

    def counting(subscription: Subscription, at: datetime) -> tuple[datetime, datetime]:
        if subscription.id == world.subscription_id:
            moved.append(at)
        return original(subscription, at)

    monkeypatch.setattr(worker_module, "current_usage_period", counting)
    # Months behind: stored April, now mid-September.
    moment = datetime(2025, 9, 14, 12, 0, tzinfo=UTC)
    await asyncio.gather(
        *(worker.run_once(now=moment) for worker in _workers(committing, monkeypatch, paymob))
    )
    subscription = await _subscription(committing, world)
    assert (subscription.usage_period_start, subscription.usage_period_end) == (
        datetime(2025, 8, 31, 8, 0, tzinfo=UTC),
        datetime(2025, 9, 30, 8, 0, tzinfo=UTC),
    )
    assert len(moved) == 1, "one reset, not one per worker"
    assert paymob.requests == [], "no provider call"
    assert await _invoices(committing, world) == [], "no invoice"
    assert (subscription.current_period_start, subscription.current_period_end) == (
        START,
        START.replace(year=START.year + 1),
    )


async def test_renewal_and_usage_roll_racing_at_the_annual_boundary_agree(
    committing: async_sessionmaker[AsyncSession],
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale cycle (June) at the boundary: the renewal opens the new year and
    its first month; the usage phase must neither open a cycle beyond the paid
    year nor a second first month."""
    async with committing() as session:
        row = await session.get(Subscription, world.subscription_id)
        assert row is not None
        row.usage_period_start = datetime(2025, 5, 31, 8, 0, tzinfo=UTC)
        row.usage_period_end = datetime(2025, 6, 30, 8, 0, tzinfo=UTC)
        await session.commit()
    paymob = Paymob()
    boundary = START.replace(year=START.year + 1)
    moment = boundary + timedelta(seconds=30)
    workers = _workers(committing, monkeypatch, paymob)
    # Half the workers start with the usage phase, half with the full sweep.
    await asyncio.gather(
        *(
            worker._advance_usage(now=moment) if index % 2 else worker.run_once(now=moment)
            for index, worker in enumerate(workers)
        )
    )
    renewals = [
        i for i in await _invoices(committing, world) if i.purpose is InvoicePurpose.RENEWAL
    ]
    assert len(renewals) == 1
    assert (renewals[0].period_start, renewals[0].period_end) == (
        boundary,
        boundary.replace(year=boundary.year + 1),
    )
    subscription = await _subscription(committing, world)
    assert (subscription.current_period_start, subscription.current_period_end) == (
        boundary,
        boundary.replace(year=boundary.year + 1),
    )
    assert (subscription.usage_period_start, subscription.usage_period_end) == (
        boundary,
        datetime(2026, 4, 30, 8, 0, tzinfo=UTC),
    ), "the new year's first month"
    async with committing() as session:
        automatic = await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.tenant_id == world.tenant_id)
            .where(Payment.is_automatic.is_(True))
        )
    assert automatic == 1 and len(paymob.pays()) == 1
