"""When a change of plan or billing term takes effect (ADR-112, ADR-116).

The matrix the specification names, decided by one pure function so checkout,
plan requests and offers cannot disagree.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.db.models.billing import BillingInterval, PlanPrice, PlanVersion
from app.services.commercial_policy import ChangeTiming, Terms, annualised, change_timing

PRO, BUSINESS, STARTER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _version(plan_id: uuid.UUID, price: str = "1") -> PlanVersion:
    return PlanVersion(id=uuid.uuid4(), plan_id=plan_id, version=1, price=Decimal(price))


def _price(version: PlanVersion, amount: str, interval: BillingInterval) -> PlanPrice:
    return PlanPrice(
        id=uuid.uuid4(),
        plan_version_id=version.id,
        billing_interval=interval,
        interval_count=1,
        amount=Decimal(amount),
        currency="EGP",
    )


PRO_V = _version(PRO)
BUSINESS_V = _version(BUSINESS)
STARTER_V = _version(STARTER, "0")
PRO_MONTHLY = _price(PRO_V, "99.00", BillingInterval.MONTHLY)
PRO_YEARLY = _price(PRO_V, "990.00", BillingInterval.YEARLY)
BUSINESS_MONTHLY = _price(BUSINESS_V, "299.00", BillingInterval.MONTHLY)
BUSINESS_YEARLY = _price(BUSINESS_V, "2990.00", BillingInterval.YEARLY)


def _like_for_like(current: PlanPrice | None, target: Terms) -> PlanPrice | None:
    """The target plan's price on the current term, as the catalogue would find it."""
    if current is None or target.price is None:
        return None
    table = {
        (PRO, BillingInterval.MONTHLY): PRO_MONTHLY,
        (PRO, BillingInterval.YEARLY): PRO_YEARLY,
        (BUSINESS, BillingInterval.MONTHLY): BUSINESS_MONTHLY,
        (BUSINESS, BillingInterval.YEARLY): BUSINESS_YEARLY,
    }
    return table.get((target.version.plan_id, current.billing_interval))


def _timing(
    current: tuple[PlanVersion, PlanPrice | None] | None,
    target: tuple[PlanVersion, PlanPrice | None],
) -> ChangeTiming:
    held = Terms(*current) if current is not None else None
    wanted = Terms(*target)
    return change_timing(
        held,
        wanted,
        target_on_current_term=_like_for_like(held.price if held else None, wanted),
    )


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        # Monthly -> yearly: a paid change now, on the same plan or a higher one.
        ((PRO_V, PRO_MONTHLY), (PRO_V, PRO_YEARLY), ChangeTiming.PURCHASE_NOW),
        ((PRO_V, PRO_MONTHLY), (BUSINESS_V, BUSINESS_YEARLY), ChangeTiming.PURCHASE_NOW),
        # Annual upgrade: a full new annual term now.
        ((PRO_V, PRO_YEARLY), (BUSINESS_V, BUSINESS_YEARLY), ChangeTiming.PURCHASE_NOW),
        # Annual downgrade, and any shortening of the term: at the term's end.
        ((BUSINESS_V, BUSINESS_YEARLY), (PRO_V, PRO_YEARLY), ChangeTiming.AT_TERM_END),
        ((BUSINESS_V, BUSINESS_YEARLY), (BUSINESS_V, BUSINESS_MONTHLY), ChangeTiming.AT_TERM_END),
        ((BUSINESS_V, BUSINESS_YEARLY), (PRO_V, PRO_MONTHLY), ChangeTiming.AT_TERM_END),
        ((PRO_V, PRO_YEARLY), (BUSINESS_V, BUSINESS_MONTHLY), ChangeTiming.AT_TERM_END),
        # A lower tier on a longer term waits too: Business monthly -> Pro yearly.
        ((BUSINESS_V, BUSINESS_MONTHLY), (PRO_V, PRO_YEARLY), ChangeTiming.AT_TERM_END),
        # Monthly upgrade and downgrade, as ADR-112 always had them.
        ((PRO_V, PRO_MONTHLY), (BUSINESS_V, BUSINESS_MONTHLY), ChangeTiming.PURCHASE_NOW),
        ((BUSINESS_V, BUSINESS_MONTHLY), (PRO_V, PRO_MONTHLY), ChangeTiming.AT_TERM_END),
        # Nothing to do: the same plan on the same term.
        ((PRO_V, PRO_MONTHLY), (PRO_V, PRO_MONTHLY), ChangeTiming.UNCHANGED),
        ((BUSINESS_V, BUSINESS_YEARLY), (BUSINESS_V, BUSINESS_YEARLY), ChangeTiming.UNCHANGED),
        # Free plans: out of free is a purchase, into free waits for the term.
        ((STARTER_V, None), (PRO_V, PRO_YEARLY), ChangeTiming.PURCHASE_NOW),
        ((BUSINESS_V, BUSINESS_YEARLY), (STARTER_V, None), ChangeTiming.AT_TERM_END),
        ((STARTER_V, None), (STARTER_V, None), ChangeTiming.UNCHANGED),
        (None, (PRO_V, PRO_MONTHLY), ChangeTiming.PURCHASE_NOW),
        (None, (STARTER_V, None), ChangeTiming.FREE_NOW),
    ],
)
def test_the_change_timing_matrix(
    current: tuple[PlanVersion, PlanPrice | None] | None,
    target: tuple[PlanVersion, PlanPrice | None],
    expected: ChangeTiming,
) -> None:
    assert _timing(current, target) is expected


def test_tiers_across_terms_are_compared_by_calendar_months_when_no_like_price_exists() -> None:
    """Pro sells only yearly here: 990 a year is compared with 299 x 12, not 299."""
    lonely = _version(uuid.uuid4())
    yearly_only = _price(lonely, "990.00", BillingInterval.YEARLY)
    assert annualised(yearly_only) == Decimal("990.00")
    assert annualised(BUSINESS_MONTHLY) == Decimal("3588.00")
    assert (
        change_timing(Terms(BUSINESS_V, BUSINESS_MONTHLY), Terms(lonely, yearly_only))
        is ChangeTiming.AT_TERM_END
    )
    richer = _price(lonely, "9990.00", BillingInterval.YEARLY)
    assert (
        change_timing(Terms(BUSINESS_V, BUSINESS_MONTHLY), Terms(lonely, richer))
        is ChangeTiming.PURCHASE_NOW
    )
