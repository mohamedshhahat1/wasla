"""The rules a plan's limits are read under.

One of them decides more than the rest: an absent limit means unlimited. Get
that backwards and every Enterprise customer is entitled to nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models.billing import (
    PERIOD_LIMITS,
    RESOURCE_LIMITS,
    SERVING_STATUSES,
    TERMINAL_SUBSCRIPTION_STATUSES,
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.services.entitlement_service import PERIOD_METERS

MOMENT = datetime(2026, 8, 23, tzinfo=UTC)


def _plan(**limits) -> Plan:
    return Plan(
        code="test",
        name="Test",
        price=Decimal("0.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={key.value: value for key, value in limits.items()},
    )


def test_every_limit_is_either_a_resource_or_a_period():
    """The two are checked by different queries, so a key belonging to neither
    would be silently unenforceable."""
    assert set(LimitKey) == RESOURCE_LIMITS | PERIOD_LIMITS
    assert not RESOURCE_LIMITS & PERIOD_LIMITS


def test_every_period_limit_knows_which_meters_it_counts():
    """A period limit without meters would read as zero used, forever."""
    assert set(PERIOD_METERS) == PERIOD_LIMITS


def test_an_absent_limit_is_unlimited():
    """What "custom limits" means for Enterprise, and the reading that must not
    be inverted: the alternative is a magic number somebody compares against."""
    plan = _plan()
    assert plan.limit_for(LimitKey.AGENTS) is None
    assert plan.limit_for(LimitKey.PERIOD_MESSAGES) is None


def test_a_stored_limit_is_returned():
    plan = _plan(**{LimitKey.AGENTS: 5})
    assert plan.limit_for(LimitKey.AGENTS) == 5


def test_zero_is_a_real_limit_of_none_at_all():
    """Starter allows no campaign messages, and that has to be expressible."""
    plan = _plan(**{LimitKey.PERIOD_CAMPAIGN_MESSAGES: 0})
    assert plan.limit_for(LimitKey.PERIOD_CAMPAIGN_MESSAGES) == 0


@pytest.mark.parametrize("value", ["5", 5.5, None, True, [5]])
def test_a_malformed_limit_is_unlimited_rather_than_zero(value):
    """A plan edited badly must not lock a paying customer out of their own
    product. Zero is never what a broken row was trying to say."""
    plan = Plan(
        code="test",
        name="Test",
        price=Decimal("0.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.AGENTS.value: value},
    )
    assert plan.limit_for(LimitKey.AGENTS) is None


def test_a_price_is_exact():
    """Money is Numeric, never float: 19.99 is not representable in binary
    floating point, and the error reaches an invoice."""
    assert Plan.__table__.columns["price"].type.python_type is Decimal


def _subscription(status: SubscriptionStatus) -> Subscription:
    return Subscription(
        tenant_id=None,
        plan_id=None,
        status=status,
        current_period_start=MOMENT,
        current_period_end=MOMENT,
    )


def test_a_workspace_being_chased_for_payment_is_still_served():
    """A failed card is a conversation to have, not a reason to cut somebody
    off mid-sentence with their own customers."""
    assert _subscription(SubscriptionStatus.PAST_DUE).is_serving is True
    assert _subscription(SubscriptionStatus.TRIALING).is_serving is True
    assert _subscription(SubscriptionStatus.ACTIVE).is_serving is True


def test_a_finished_subscription_serves_nobody():
    assert _subscription(SubscriptionStatus.CANCELLED).is_serving is False
    assert _subscription(SubscriptionStatus.EXPIRED).is_serving is False


def test_serving_and_terminal_do_not_overlap():
    assert not SERVING_STATUSES & TERMINAL_SUBSCRIPTION_STATUSES
    assert set(SubscriptionStatus) == SERVING_STATUSES | TERMINAL_SUBSCRIPTION_STATUSES


def test_one_subscription_per_workspace_is_the_databases_job():
    names = {constraint.name for constraint in Subscription.__table__.constraints}
    assert "uq_subscriptions_tenant_id" in names
