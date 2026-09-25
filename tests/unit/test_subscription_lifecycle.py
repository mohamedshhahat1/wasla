"""The rules a subscription changes state under.

No database: what is asserted here is the arithmetic and the decisions, both of
which are pure. `roll_over` in particular is deliberately a function over a row
rather than a method that queries, so the three outcomes can be pinned without
standing up a period that has already ended.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.services.subscription_service import add_interval, roll_over

JANUARY_31 = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _plan(interval: BillingInterval = BillingInterval.MONTHLY, *, price: str = "99.00") -> Plan:
    return Plan(
        code="pro" if Decimal(price) > 0 else "starter",
        name="Pro" if Decimal(price) > 0 else "Starter",
        price=Decimal(price),
        currency="EGP",
        interval=interval,
        limits={},
    )


def _subscription(
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    *,
    cancel_at_period_end: bool = False,
    start: datetime = datetime(2026, 7, 23, tzinfo=UTC),
    end: datetime = datetime(2026, 8, 23, tzinfo=UTC),
) -> Subscription:
    return Subscription(
        tenant_id=None,
        plan_id=None,
        status=status,
        current_period_start=start,
        current_period_end=end,
        cancel_at_period_end=cancel_at_period_end,
    )


# ------------------------------------------------------------------ intervals


def test_a_monthly_period_is_the_same_date_next_month() -> None:
    """Not 30 days: a fixed count drifts a renewal backwards through the year
    until it lands in the wrong month entirely."""
    assert add_interval(NOW, BillingInterval.MONTHLY) == datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_the_thirty_first_renews_on_the_last_day_of_a_shorter_month() -> None:
    """February has no 31st, and a renewal has to happen anyway."""
    assert add_interval(JANUARY_31, BillingInterval.MONTHLY) == datetime(
        2026, 2, 28, 9, 0, tzinfo=UTC
    )


def test_december_rolls_into_the_next_year() -> None:
    december = datetime(2026, 12, 15, tzinfo=UTC)
    assert add_interval(december, BillingInterval.MONTHLY) == datetime(2027, 1, 15, tzinfo=UTC)


def test_a_yearly_period_is_the_same_date_next_year() -> None:
    assert add_interval(NOW, BillingInterval.YEARLY) == datetime(2027, 8, 23, 12, 0, tzinfo=UTC)


def test_the_twenty_ninth_of_february_renews_on_the_twenty_eighth() -> None:
    leap_day = datetime(2028, 2, 29, tzinfo=UTC)
    assert add_interval(leap_day, BillingInterval.YEARLY) == datetime(2029, 2, 28, tzinfo=UTC)


# ------------------------------------------------------------------ roll-over


@pytest.mark.asyncio
async def test_a_pending_cancellation_takes_effect_when_the_period_ends() -> None:
    subscription = _subscription(cancel_at_period_end=True)

    await roll_over(subscription, plan=_plan(), now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.ended_at == NOW


@pytest.mark.asyncio
async def test_a_trial_nobody_acted_on_expires_rather_than_cancels() -> None:
    """Nobody chose this, and the two want different emails."""
    subscription = _subscription(SubscriptionStatus.TRIALING)

    await roll_over(subscription, plan=_plan(), now=NOW)

    assert subscription.status is SubscriptionStatus.EXPIRED


@pytest.mark.asyncio
async def test_a_legacy_free_trial_rolls_on_as_active_rather_than_expiring() -> None:
    """BILL-01 (mutation R-BILL-01). A free plan's 'trial' grants nothing to end.

    No new subscription trials a free plan, but one written before migration
    0070 - or restored from an old backup - can still say `trialing`. At its
    period end it becomes plain `active` on the same plan and rolls on; it
    used to become `expired`, after which a paid checkout took the money and
    granted nothing.
    """
    subscription = _subscription(SubscriptionStatus.TRIALING)
    subscription.trial_ends_at = datetime(2026, 8, 23, tzinfo=UTC)

    await roll_over(subscription, plan=_plan(price="0.00"), now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.ended_at is None
    assert subscription.current_period_start == datetime(2026, 8, 23, tzinfo=UTC)
    assert subscription.trial_ends_at is None


@pytest.mark.asyncio
async def test_an_active_subscription_opens_the_next_period() -> None:
    subscription = _subscription(
        start=datetime(2026, 7, 23, tzinfo=UTC),
        end=datetime(2026, 8, 23, tzinfo=UTC),
    )

    await roll_over(subscription, plan=_plan(), now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    # The new period starts where the old one ended, not "now": otherwise a
    # sweep that runs late silently shortens the customer's month.
    assert subscription.current_period_start == datetime(2026, 8, 23, tzinfo=UTC)
    assert subscription.current_period_end == datetime(2026, 9, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_a_month_end_anchor_survives_february_through_roll_over() -> None:
    """BILL-18 (mutation R-BILL-18): the sweep counts from the anchor, not the last end.

    Bought on 31 January, renewed on 28 February: the next period must end on
    31 March. Chaining from 28 February used to fix it on the 28th for ever.
    """
    subscription = _subscription(
        start=datetime(2026, 1, 31, 9, tzinfo=UTC),
        end=datetime(2026, 2, 28, 9, tzinfo=UTC),
    )
    subscription.billing_anchor_at = datetime(2026, 1, 31, 9, tzinfo=UTC)

    await roll_over(subscription, plan=_plan(), now=datetime(2026, 2, 28, 9, 5, tzinfo=UTC))
    assert subscription.current_period_end == datetime(2026, 3, 31, 9, tzinfo=UTC)

    await roll_over(subscription, plan=_plan(), now=datetime(2026, 3, 31, 9, 5, tzinfo=UTC))
    assert subscription.current_period_end == datetime(2026, 4, 30, 9, tzinfo=UTC)


@pytest.mark.asyncio
async def test_an_unpaid_subscription_keeps_its_state_into_the_next_period() -> None:
    """A new period does not settle an old debt, and quietly marking it active
    would lose the fact that somebody still owes money."""
    subscription = _subscription(SubscriptionStatus.PAST_DUE)

    await roll_over(subscription, plan=_plan(), now=NOW)

    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert subscription.ended_at is None


@pytest.mark.asyncio
async def test_a_cancellation_wins_over_a_trial_ending() -> None:
    """Somebody asked to leave. That is a decision, and it outranks a deadline."""
    subscription = _subscription(SubscriptionStatus.TRIALING, cancel_at_period_end=True)

    await roll_over(subscription, plan=_plan(), now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
