"""When a change of plan or price takes effect (ADR-112, ADR-116).

One decision, asked by the checkout, by a customer's plan change and by an
offer's acceptance, so the three can never disagree:

- **A purchase now.** The customer pays the full new price at a checkout and,
  on settlement, a new billing term starts - no credit, no proration (ADR-112).
  Upgrading to a higher tier, and lengthening the term of the same plan
  (monthly -> yearly), are purchases.
- **At the end of the paid term.** Nothing already paid for is forfeited: the
  change is scheduled, pinned to its exact version and price, and the renewal
  at the boundary bills it. A lower tier, and *shortening* the term (yearly ->
  monthly), wait - discarding eleven paid months to start a monthly term
  would take what the customer bought.
- **Free, now.** Between free plans nothing is paid, so nothing waits.
- **Nothing to do.** The same plan on the same term.

**Comparing tiers across terms.** Monthly Pro against yearly Business cannot be
compared by amount: a year costs more than a month of anything. The target is
priced *on the current term* when it sells one (Business's own monthly price
against Pro's), and otherwise both are annualised - never by days, always by
calendar months. A tie is not a downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.db.models.billing import PlanPrice, PlanVersion

_MONTHS_PER_YEAR: Final = Decimal(12)


class ChangeTiming(StrEnum):
    """What a requested change of terms means for the customer."""

    PURCHASE_NOW = "purchase_now"
    AT_TERM_END = "at_term_end"
    FREE_NOW = "free_now"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class Terms:
    """What a subscription holds, or what a customer asks for."""

    version: PlanVersion
    price: PlanPrice | None

    @property
    def is_free(self) -> bool:
        return self.price is None

    @property
    def months(self) -> int:
        """The billing term in months; a free plan's month is not a paid term."""
        return self.price.months if self.price is not None else 0


def annualised(price: PlanPrice) -> Decimal:
    """What a price costs over twelve calendar months."""
    return price.amount * _MONTHS_PER_YEAR / Decimal(price.months)


def change_timing(
    current: Terms | None,
    target: Terms,
    *,
    target_on_current_term: PlanPrice | None = None,
) -> ChangeTiming:
    """When moving from `current` to `target` takes effect. See the module.

    `target_on_current_term` is the target version's active price on the
    current price's term, when the caller found one - what makes a tier
    comparison across terms a like-for-like one.
    """
    same_plan = current is not None and current.version.plan_id == target.version.plan_id
    held = current.price if current is not None else None
    wanted = target.price
    if wanted is None:
        if held is None:
            return ChangeTiming.UNCHANGED if same_plan else ChangeTiming.FREE_NOW
        return ChangeTiming.AT_TERM_END
    if held is None:
        return ChangeTiming.PURCHASE_NOW

    if wanted.months < held.months:
        # Shortening the term never discards what was paid for.
        return ChangeTiming.AT_TERM_END
    if same_plan:
        return ChangeTiming.PURCHASE_NOW if wanted.months > held.months else ChangeTiming.UNCHANGED
    if _lower_tier(held, wanted, target_on_current_term):
        return ChangeTiming.AT_TERM_END
    return ChangeTiming.PURCHASE_NOW


def _lower_tier(
    current: PlanPrice,
    target: PlanPrice,
    target_on_current_term: PlanPrice | None,
) -> bool:
    if target_on_current_term is not None:
        return target_on_current_term.amount < current.amount
    if target.months == current.months:
        return target.amount < current.amount
    return annualised(target) < annualised(current)


__all__ = ["ChangeTiming", "Terms", "annualised", "change_timing"]
