"""Billing API contracts.

Limits are returned as a list of entitlements rather than as the plan's raw
dictionary, because what a client actually needs is "how many, how many used,
and may I do one more" — and the raw JSONB answers only the first of those.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class CheckoutRequestPayload(BaseModel):
    """Starting a hosted checkout, for a plan or for an invoice already due.

    Identifiers and nothing else, and that is the security property rather than
    a minimal API. There is deliberately no `amount`, no `currency` and no
    workspace: every one of those is read from the database and the
    authenticated session, so a client cannot ask to be charged a figure of its
    choosing. `extra="forbid"` makes an attempt to send one a 422 rather than a
    field quietly ignored.

    `invoice_id` is how a renewal gets paid. The invoice is still looked up
    tenant-scoped, so naming another workspace's is a not-found rather than a
    bill somebody else can settle.

    `idempotency_key` lets a caller say "this is the same request as before" so
    a retry does not open a second payment page. A repeat is refused rather
    than replayed - the response carries a one-use URL that is deliberately
    never stored, so there is nothing honest to replay.
    """

    model_config = ConfigDict(extra="forbid")

    plan_code: str | None = Field(default=None, min_length=1, max_length=MAX_PLAN_CODE_INPUT)
    invoice_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exactly_one_subject(self) -> Self:
        """One or the other. Both, or neither, is a caller that has not decided.

        Refused here rather than in the service so the answer is a 422 naming
        the field, which is what a client can act on - and so the service's own
        check stays as the guarantee rather than as the error message.
        """
        if (self.plan_code is None) == (self.invoice_id is None):
            raise ValueError("Name either plan_code or invoice_id, not both.")
        return self


class CheckoutStarted(BaseModel):
    """Where to send the customer, and what they are about to pay.

    The amount is echoed so a client can show it before redirecting, and it is
    the server's figure - a client that displays this is displaying what will
    actually be charged.

    The provider's client secret is not here. It travels inside
    `redirect_url` because the customer's browser has to carry it, and putting
    it in a field of its own would invite a client to store or log it.
    """

    redirect_url: str
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    currency: str


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
