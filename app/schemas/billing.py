"""Billing API contracts.

Limits are returned as a list of entitlements rather than as the plan's raw
dictionary, because what a client actually needs is "how many, how many used,
and may I do one more" — and the raw JSONB answers only the first of those.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models.billing import (
    METER_ONLY_LIMITS,
    RESOURCE_LIMITS,
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    PlanVersion,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.channel import Channel
from app.schemas.text import StorableText
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_service import Entitlement, EntitlementService
from app.services.entitlement_terms import ordered, term_channel_types, term_limit

MAX_PLAN_CODE_INPUT = 50


class PlanLimitRead(BaseModel):
    """One limit on a plan. A null ceiling means unlimited."""

    key: LimitKey
    limit: int | None


class PlanPriceOptionRead(BaseModel):
    """One way to pay for a plan: an amount per billing term (ADR-116).

    What a customer chooses at checkout, by its `id`. `interval` is the
    billing term - `monthly` or `yearly` - and never how often allowances
    reset: those reset every calendar month on either.
    """

    id: str
    interval: BillingInterval
    interval_count: int
    amount: str
    currency: str

    @classmethod
    def from_model(cls, price: PlanPrice) -> Self:
        return cls(
            id=str(price.id),
            interval=price.billing_interval,
            interval_count=price.interval_count,
            amount=f"{price.amount:.2f}",
            currency=price.currency,
        )


class PlanRead(BaseModel):
    """A plan as a pricing page shows it: one version, and every price of it.

    `version` names which immutable entitlements these are (BILL-12). On the
    catalogue it is the version a new customer would buy; on a subscription it
    is the version that subscriber is held to - which is not necessarily the
    same thing, and that is the point.

    `prices` are the price options a customer may choose - monthly, yearly or
    both - each granting exactly these limits (ADR-116). `billing_required` is
    false for a free plan, which has no prices and is never checked out.
    `price`, `currency` and `interval` are kept for earlier clients: the
    default (monthly) price where there is one. A checkout names a price id.
    """

    id: str
    code: str
    name: str
    description: str | None
    version: int | None
    price: Decimal
    currency: str
    interval: BillingInterval
    billing_required: bool = True
    prices: list[PlanPriceOptionRead] = Field(default_factory=list)
    trial_days: int
    limits: list[PlanLimitRead]
    # The channel types this plan includes (ENT-09), as enforced: a version
    # published before ADR-131 includes WhatsApp alone.
    allowed_channel_types: list[Channel] = Field(default_factory=list)
    # Written for this workspace alone (ADR-113). Never true for a plan another
    # workspace could see.
    is_custom: bool = False

    @classmethod
    def from_model(
        cls,
        plan: Plan,
        version: PlanVersion | None = None,
        *,
        prices: list[PlanPrice] | None = None,
    ) -> Self:
        terms: Plan | PlanVersion = version if version is not None else plan
        options = list(prices or [])
        default = next(
            (
                price
                for price in options
                if price.billing_interval is BillingInterval.MONTHLY and price.interval_count == 1
            ),
            options[0] if options else None,
        )
        return cls(
            id=str(plan.id),
            code=plan.code,
            name=version.name if version is not None else plan.name,
            description=plan.description,
            version=version.version if version is not None else None,
            price=default.amount if default is not None else terms.price,
            currency=default.currency if default is not None else terms.currency,
            interval=default.billing_interval if default is not None else terms.interval,
            billing_required=terms.price > 0,
            prices=[PlanPriceOptionRead.from_model(price) for price in options],
            trial_days=terms.trial_days,
            # Every key, including the ones this plan does not limit, so a
            # comparison table renders "unlimited" rather than a blank cell it
            # has to guess the meaning of.
            limits=[PlanLimitRead(key=key, limit=term_limit(terms, key)) for key in LimitKey],
            allowed_channel_types=ordered(term_channel_types(terms)),
            is_custom=plan.is_custom,
        )


class ChannelCountRead(BaseModel):
    """A count for one channel."""

    channel: Channel
    count: int


class ChannelUsageRead(BaseModel):
    """One channel's share of a workspace-wide meter (ENT-01). Display only.

    `channel` is null for charges recorded before the channel dimension
    existed. No limit is ever compared with one channel's figure.
    """

    channel: Channel | None
    used: int


class TypedSlotRead(BaseModel):
    """Slots only one channel type may use (ENT-11): how many, and how many are taken."""

    channel: Channel
    capacity: int
    used: int


class ChannelCapacityBreakdown(BaseModel):
    """Where a workspace's channel slots come from, and what takes them (ENT-05, ENT-11).

    `general_limit` is the plan's slots plus general top-ups and grants - usable
    by any allowed type; `typed_slots` are bought or granted for one type.
    `remaining` on the entitlement is what is left for the next connection of a
    type with no typed slot of its own.
    """

    general_limit: int | None
    general_topup_limit: int
    general_platform_grant_limit: int
    typed_slots: list[TypedSlotRead]
    active_by_channel: list[ChannelCountRead]
    allowed_channel_types: list[Channel]

    @classmethod
    def from_capacity(cls, capacity: ChannelCapacity) -> Self:
        typed = capacity.typed
        return cls(
            general_limit=capacity.general,
            general_topup_limit=capacity.general_purchased,
            general_platform_grant_limit=capacity.general_granted,
            typed_slots=[
                TypedSlotRead(
                    channel=channel, capacity=typed[channel], used=capacity.typed_used(channel)
                )
                for channel in ordered(typed)
            ],
            active_by_channel=[
                ChannelCountRead(channel=channel, count=capacity.active[channel])
                for channel in ordered(capacity.active)
            ],
            allowed_channel_types=ordered(capacity.allowed),
        )


class EntitlementRead(BaseModel):
    """Where a workspace stands against one limit.

    `limit` and `remaining` are both null when unlimited. Null rather than a
    large number: a client that renders "999999 left" has been told something
    false.

    `limit` is the effective limit and equals `effective_limit` (ADR-113):

        effective_limit = base_limit + topup_limit + platform_grant_limit

    `over_limit` is true when the workspace holds or has used more than it is
    now allowed - after a capacity top-up expired, say. Nothing is deleted;
    `remaining` reads zero, never a negative number, and adding more is refused
    until usage fits. `enforced` is false for the one meter-only key,
    `period_messages`, which no customer message is ever refused over.
    `period_start`/`period_end` bound a usage key's count and are null for
    capacities.

    `period_ai_turns` is one allowance for the whole workspace (ENT-01):
    `used` counts every channel's charges, `held` the turns engaged and still
    generating (spoken for, so `remaining` leaves them out - ENT-03), and
    `used_by_channel` breaks `used` down for display only.
    """

    key: LimitKey
    kind: Literal["usage", "capacity"]
    enforced: bool
    limit: int | None
    base_limit: int | None
    topup_limit: int
    platform_grant_limit: int
    effective_limit: int | None
    used: int
    remaining: int | None
    over_limit: bool
    allowed: bool
    period_start: datetime | None
    period_end: datetime | None
    # `period_ai_turns` only: open holds, zero elsewhere (ENT-03).
    held: int = 0
    # `period_ai_turns` only: `used` by channel, for display (ENT-01).
    used_by_channel: list[ChannelUsageRead] | None = None
    # `channel_connections` only (ADR-131): its general and typed slots, active
    # connections by channel and the plan's allowed channel types.
    channel_capacity: ChannelCapacityBreakdown | None = None

    @classmethod
    def from_entitlement(
        cls,
        entitlement: Entitlement,
        *,
        by_channel: Mapping[Channel | None, int] | None = None,
    ) -> Self:
        return cls(
            key=entitlement.key,
            kind="capacity" if entitlement.key in RESOURCE_LIMITS else "usage",
            enforced=entitlement.key not in METER_ONLY_LIMITS,
            limit=entitlement.limit,
            base_limit=entitlement.base_limit,
            topup_limit=entitlement.topup_limit,
            platform_grant_limit=entitlement.grant_limit,
            effective_limit=entitlement.limit,
            used=entitlement.used,
            remaining=entitlement.remaining,
            over_limit=entitlement.over_limit,
            allowed=entitlement.allowed,
            period_start=entitlement.period_start,
            period_end=entitlement.period_end,
            held=entitlement.held,
            used_by_channel=(
                [
                    ChannelUsageRead(channel=channel, used=used)
                    for channel, used in sorted(
                        by_channel.items(),
                        key=lambda item: (item[0] is None, item[0].value if item[0] else ""),
                    )
                ]
                if by_channel is not None
                else None
            ),
            channel_capacity=(
                ChannelCapacityBreakdown.from_capacity(entitlement.capacity)
                if entitlement.capacity is not None
                else None
            ),
        )


async def entitlement_reads(
    entitlements: EntitlementService, keys: Iterable[LimitKey] | None = None
) -> list[EntitlementRead]:
    """A workspace's entitlements as the API reads them; AI turns carry `used_by_channel`.

    The one place the tenant route and the platform summary build their
    entitlement list, so the two can never show different figures.
    """
    reads = []
    for entitlement in await entitlements.snapshot(keys):
        by_channel = (
            await entitlements.ai_turns_by_channel()
            if entitlement.key is LimitKey.PERIOD_AI_TURNS
            else None
        )
        reads.append(EntitlementRead.from_entitlement(entitlement, by_channel=by_channel))
    return reads


class ScheduledChangeRead(BaseModel):
    """A plan or price change waiting for the current billing term to end.

    Pinned to its exact price (ADR-116): yearly to monthly shows the monthly
    price the next invoice will charge, at `effective_at`, the end of the
    paid year.
    """

    plan_version_id: str
    plan_price_id: str | None = None
    billing_interval: BillingInterval | None = None
    interval_count: int | None = None
    amount: str | None = None
    currency: str | None = None
    source: ScheduledChangeSource
    effective_at: datetime


class SubscriptionRead(BaseModel):
    """A workspace's subscription, with the terms it is held to.

    Two periods, never one (ADR-116):

    - **billing term** - `billing_period_start`/`billing_period_end` (also
      `current_period_start`/`current_period_end`, their earlier names): what
      the last payment covers. A year on a yearly price; `paid_through` is its
      end.
    - **usage cycle** - `usage_period_start`/`usage_period_end`: the calendar
      month the usage allowances are counted over. Inside a yearly term it is
      one of twelve.

    `plan_price` is the price the subscription renews at; `next_renewal_at` and
    `next_renewal_amount` say when and what the next invoice will be - null
    when nothing will renew (a cancellation at the term's end).
    """

    id: str
    status: SubscriptionStatus
    plan: PlanRead
    plan_version_id: str | None
    plan_price: PlanPriceOptionRead | None = None
    billing_interval: BillingInterval | None = None
    current_period_start: datetime
    current_period_end: datetime
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None
    paid_through: datetime | None = None
    usage_period_start: datetime | None = None
    usage_period_end: datetime | None = None
    next_renewal_at: datetime | None = None
    next_renewal_amount: str | None = None
    next_renewal_currency: str | None = None
    billing_anchor_at: datetime | None
    trial_ends_at: datetime | None
    cancel_at_period_end: bool
    cancelled_at: datetime | None
    ended_at: datetime | None
    scheduled_change: ScheduledChangeRead | None
    revision: int

    @classmethod
    def from_model(
        cls,
        subscription: Subscription,
        *,
        plan: Plan,
        version: PlanVersion | None = None,
        price: PlanPrice | None = None,
        scheduled_price: PlanPrice | None = None,
        prices: list[PlanPrice] | None = None,
        usage_period: tuple[datetime, datetime] | None = None,
    ) -> Self:
        scheduled = None
        if subscription.scheduled_plan_version_id is not None:
            scheduled = ScheduledChangeRead(
                plan_version_id=str(subscription.scheduled_plan_version_id),
                plan_price_id=str(scheduled_price.id) if scheduled_price is not None else None,
                billing_interval=(
                    scheduled_price.billing_interval if scheduled_price is not None else None
                ),
                interval_count=(
                    scheduled_price.interval_count if scheduled_price is not None else None
                ),
                amount=f"{scheduled_price.amount:.2f}" if scheduled_price is not None else None,
                currency=scheduled_price.currency if scheduled_price is not None else None,
                source=subscription.scheduled_change_source or ScheduledChangeSource.OPERATOR,
                effective_at=subscription.current_period_end,
            )
        renews = not subscription.is_terminal and not subscription.cancel_at_period_end
        renewing_at = scheduled_price if scheduled is not None else price
        usage_start, usage_end = (
            usage_period
            if usage_period is not None
            else (subscription.usage_period_start, subscription.usage_period_end)
        )
        return cls(
            id=str(subscription.id),
            status=subscription.status,
            plan=PlanRead.from_model(plan, version, prices=prices),
            plan_version_id=(
                str(subscription.plan_version_id) if subscription.plan_version_id else None
            ),
            plan_price=PlanPriceOptionRead.from_model(price) if price is not None else None,
            billing_interval=price.billing_interval if price is not None else None,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            billing_period_start=subscription.current_period_start,
            billing_period_end=subscription.current_period_end,
            paid_through=subscription.current_period_end if price is not None else None,
            usage_period_start=usage_start,
            usage_period_end=usage_end,
            next_renewal_at=subscription.current_period_end if renews else None,
            next_renewal_amount=(
                f"{renewing_at.amount:.2f}"
                if renews and renewing_at is not None
                else ("0.00" if renews else None)
            ),
            next_renewal_currency=(
                renewing_at.currency if renews and renewing_at is not None else None
            ),
            billing_anchor_at=subscription.billing_anchor_at,
            trial_ends_at=subscription.trial_ends_at,
            cancel_at_period_end=subscription.cancel_at_period_end,
            cancelled_at=subscription.cancelled_at,
            ended_at=subscription.ended_at,
            scheduled_change=scheduled,
            revision=subscription.revision or 1,
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
    """Choosing a plan or a price.

    `plan_price_id` names the exact price - and billing term - wanted, as
    `GET /billing/plans` lists them (ADR-116). `plan_code` alone means that
    plan's default (monthly) price, as it always did. Never an amount, a
    currency or an interval: those are the price's own.
    """

    model_config = ConfigDict(extra="forbid")

    plan_code: StorableText | None = Field(
        default=None, min_length=1, max_length=MAX_PLAN_CODE_INPUT
    )
    plan_price_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _names_something(self) -> Self:
        if self.plan_code is None and self.plan_price_id is None:
            raise ValueError("Name plan_price_id or plan_code.")
        return self


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

    # The price to buy (ADR-116): monthly or yearly, as `GET /billing/plans`
    # lists them. The amount, currency and billing term are the price's own.
    plan_price_id: uuid.UUID | None = None
    # Earlier clients: a plan code alone buys that plan's monthly price.
    plan_code: StorableText | None = Field(
        default=None, min_length=1, max_length=MAX_PLAN_CODE_INPUT
    )
    invoice_id: uuid.UUID | None = None
    idempotency_key: StorableText | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exactly_one_subject(self) -> Self:
        """Exactly one subject. Two, or none, is a caller that has not decided.

        Refused here rather than in the service so the answer is a 422 naming
        the field, which is what a client can act on - and so the service's own
        check stays as the guarantee rather than as the error message.
        """
        named = [self.plan_price_id, self.plan_code, self.invoice_id]
        if sum(value is not None for value in named) != 1:
            raise ValueError("Name exactly one of plan_price_id, plan_code or invoice_id.")
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
    "PlanPriceOptionRead",
    "PlanRead",
    "PlanSelectionRequest",
    "SubscriptionRead",
    "SubscriptionStateRead",
]
