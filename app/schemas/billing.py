"""Billing API contracts.

Limits are returned as a list of entitlements rather than as the plan's raw
dictionary, because what a client actually needs is "how many, how many used,
and may I do one more" — and the raw JSONB answers only the first of those.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.services.entitlement_service import Entitlement

MAX_PLAN_CODE_INPUT = 50


class PlanLimitRead(BaseModel):
    """One limit on a plan. A null ceiling means unlimited."""

    key: LimitKey
    limit: int | None


class PlanRead(BaseModel):
    """A plan as a pricing page shows it."""

    id: str
    code: str
    name: str
    description: str | None
    price: Decimal
    currency: str
    interval: BillingInterval
    trial_days: int
    limits: list[PlanLimitRead]

    @classmethod
    def from_model(cls, plan: Plan) -> Self:
        return cls(
            id=str(plan.id),
            code=plan.code,
            name=plan.name,
            description=plan.description,
            price=plan.price,
            currency=plan.currency,
            interval=plan.interval,
            trial_days=plan.trial_days,
            # Every key, including the ones this plan does not limit, so a
            # comparison table renders "unlimited" rather than a blank cell it
            # has to guess the meaning of.
            limits=[PlanLimitRead(key=key, limit=plan.limit_for(key)) for key in LimitKey],
        )


class EntitlementRead(BaseModel):
    """Where a workspace stands against one limit.

    `limit` and `remaining` are both null when unlimited. Null rather than a
    large number: a client that renders "999999 left" has been told something
    false.
    """

    key: LimitKey
    limit: int | None
    used: int
    remaining: int | None
    allowed: bool

    @classmethod
    def from_entitlement(cls, entitlement: Entitlement) -> Self:
        return cls(
            key=entitlement.key,
            limit=entitlement.limit,
            used=entitlement.used,
            remaining=entitlement.remaining,
            allowed=entitlement.allowed,
        )


class SubscriptionRead(BaseModel):
    """A workspace's subscription, with the plan it is on."""

    id: str
    status: SubscriptionStatus
    plan: PlanRead
    current_period_start: datetime
    current_period_end: datetime
    trial_ends_at: datetime | None
    cancel_at_period_end: bool
    cancelled_at: datetime | None
    ended_at: datetime | None

    @classmethod
    def from_model(cls, subscription: Subscription, *, plan: Plan) -> Self:
        return cls(
            id=str(subscription.id),
            status=subscription.status,
            plan=PlanRead.from_model(plan),
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            trial_ends_at=subscription.trial_ends_at,
            cancel_at_period_end=subscription.cancel_at_period_end,
            cancelled_at=subscription.cancelled_at,
            ended_at=subscription.ended_at,
        )


class SubscriptionStateRead(BaseModel):
    """What a billing page needs in one request.

    `subscription` is null for a workspace that has never chosen a plan, and
    `entitlements` is still populated: it is answering from the default plan,
    which is exactly what that workspace is being held to.
    """

    subscription: SubscriptionRead | None
    entitlements: list[EntitlementRead]


class PlanSelectionRequest(BaseModel):
    """Choosing a plan, by its stable code."""

    model_config = ConfigDict(extra="forbid")

    plan_code: str = Field(min_length=1, max_length=MAX_PLAN_CODE_INPUT)


class CancellationRequest(BaseModel):
    """Ending a subscription.

    The default is at the end of the period the customer has paid for. Ending it
    the instant they click takes something they bought, and is also what makes
    people afraid to click.
    """

    model_config = ConfigDict(extra="forbid")

    immediately: bool = False


__all__ = [
    "CancellationRequest",
    "EntitlementRead",
    "PlanLimitRead",
    "PlanRead",
    "PlanSelectionRequest",
    "SubscriptionRead",
    "SubscriptionStateRead",
]
