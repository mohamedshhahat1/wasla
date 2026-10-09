"""Contracts for the platform billing control plane (BILL-12).

Everything an operator needs to run billing without SQL: the plan catalogue and
its versions, subscriptions, invoices, payments, refunds and reconciliation.

Three rules hold across every request here:

* **Every mutation carries a reason.** It is written to the audit trail beside
  the actor, the before and the after.
* **Every mutation of an existing row carries the revision it was based on.**
  A stale edit is a 409, never a silent last-write-wins (spec: optimistic
  concurrency). Plan versions use `expected_version`, the version number the
  operator last saw.
* **Money is validated where it enters.** Prices and limits are refused when
  negative, out of range or in a currency this product cannot bill in; unknown
  entitlement keys are refused rather than stored as dead configuration.

No response here carries a card token, its envelope or fingerprint, a Paymob
secret, a payment key or a client secret.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.db.models.billing import (
    DEFAULT_CURRENCY,
    MAX_LIMIT_VALUE,
    MAX_PLAN_PRICE,
    RETIRED_LIMIT_KEY,
    SUPPORTED_CURRENCIES,
    SUPPORTED_INTERVAL_COUNTS,
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    PlanScope,
    PlanVersion,
    PlanVersionMigration,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.billing_incident import (
    BillingIncident,
    BillingIncidentKind,
    BillingIncidentStatus,
)
from app.db.models.channel import Channel
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.schemas.text import StorableText
from app.services.entitlement_terms import (
    is_legacy,
    ordered,
    term_channel_types,
    term_limit,
)

MAX_PAGE = 100
PlanCode = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
Reason = Field(min_length=3, max_length=500)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


#: The closed shape of a limits object, published so a client (and the request
#: size guard) sees every key it may send rather than a free-form map.
LIMITS_SCHEMA: dict[str, Any] = {
    "properties": {
        key.value: {
            "anyOf": [
                {"type": "integer", "minimum": 0, "maximum": MAX_LIMIT_VALUE},
                {"type": "null"},
            ]
        }
        for key in LimitKey
    },
    "additionalProperties": False,
}


def validate_limits(value: dict[str, int | None]) -> dict[str, int | None]:
    """Known keys only; `null` is unlimited; zero is zero; negative is refused.

    `whatsapp_numbers` is retired (ENT-05): it is refused by name, pointing at
    `channel_connections`, rather than as an unknown key.
    """
    known = {key.value for key in LimitKey}
    for key, limit in value.items():
        if key == RETIRED_LIMIT_KEY:
            raise ValueError(
                "whatsapp_numbers is retired: every channel, WhatsApp included, is counted "
                "by channel_connections."
            )
        if key not in known:
            raise ValueError(f"Unknown entitlement key: {key}.")
        if limit is None:
            continue
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"The limit for {key} must be a whole number or null.")
        if limit < 0:
            raise ValueError(f"The limit for {key} cannot be negative.")
        if limit > MAX_LIMIT_VALUE:
            raise ValueError(f"The limit for {key} is too large; use null for unlimited.")
    return value


def validate_channel_types(value: list[Channel]) -> list[Channel]:
    """Each channel type once, in vocabulary order (ENT-09)."""
    if len(value) != len(set(value)):
        raise ValueError("Each allowed channel type is named once.")
    return ordered(value)


def _interval(value: object) -> object:
    """`month` and `year` are accepted as the stored `monthly` and `yearly`."""
    return (
        {"month": "monthly", "year": "yearly"}.get(value, value)
        if isinstance(value, str)
        else value
    )


#: A billing term's unit, in either spelling an operator is likely to write.
IntervalField = Annotated[BillingInterval, BeforeValidator(_interval)]


def _supported_currency(value: str) -> str:
    upper = value.upper()
    if upper not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Only {', '.join(sorted(SUPPORTED_CURRENCIES))} is supported.")
    return upper


class PriceSpec(BaseModel):
    """One price of a plan version: an amount per billing term (ADR-116).

    Validated identically wherever a price is written - on its own, with a new
    version, with a custom plan. The amount is the operator's, never a
    customer's: no customer-facing request carries one.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    billing_interval: IntervalField = Field(
        validation_alias=AliasChoices("billing_interval", "interval")
    )
    # Only 1 is sold. The column exists so a quarterly or two-year price needs
    # no new model; selling one is a product decision not yet taken.
    interval_count: int = Field(default=1)
    amount: Decimal = Field(gt=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2)
    currency: StorableText = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)

    @field_validator("interval_count")
    @classmethod
    def _count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("interval_count must be at least 1.")
        if value not in SUPPORTED_INTERVAL_COUNTS:
            raise ValueError("Only a one-month or one-year term is sold; interval_count must be 1.")
        return value

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        return _supported_currency(value)

    @property
    def slot(self) -> tuple[BillingInterval, int, str]:
        return self.billing_interval, self.interval_count, self.currency


class _Terms(BaseModel):
    """Commercial terms, validated identically wherever they are written.

    Prices are given one of two ways, never both:

    - `prices`: every term the version is sold on (ADR-116) - monthly, yearly
      or both. An empty list is a free version.
    - `price` and `interval`: one price, the shape every earlier client sends.
      A price of 0 is a free version.

    The limits are the version's and are the same whichever price is paid.

    `allowed_channel_types` is required (ENT-09): the channel types a workspace
    on this version may connect and automate, as `Channel` labels. There is no
    wildcard - "every channel" is all five labels, named - and an empty list is
    an explicit "no channel". Adding a channel to a plan is a new version.
    """

    model_config = ConfigDict(extra="forbid")

    price: Decimal | None = Field(
        default=None, ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2
    )
    currency: StorableText = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    interval: IntervalField | None = None
    prices: list[PriceSpec] | None = Field(default=None, max_length=4)
    # Keyed by entitlement key. `null` (or an absent key) is unlimited.
    limits: dict[str, int | None] = Field(default_factory=dict, json_schema_extra=LIMITS_SCHEMA)
    allowed_channel_types: list[Channel] = Field(max_length=len(Channel))
    # Trials are not implemented: a free plan never needs one and a priced
    # plan's trial never applied (BILL-23). Zero is the only accepted value.
    trial_days: Literal[0] = 0
    effective_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        return _supported_currency(value)

    @field_validator("limits")
    @classmethod
    def _limits(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        return validate_limits(value)

    @field_validator("allowed_channel_types")
    @classmethod
    def _channel_types(cls, value: list[Channel]) -> list[Channel]:
        return validate_channel_types(value)

    @model_validator(mode="after")
    def _one_way_of_pricing(self) -> Self:
        if self.prices is None:
            if self.price is None or self.interval is None:
                raise ValueError("Give prices, or price and interval.")
            return self
        if self.price is not None or self.interval is not None:
            raise ValueError("Give prices, or price and interval - not both.")
        slots = [price.slot for price in self.prices]
        if len(slots) != len(set(slots)):
            raise ValueError("Each billing term may be priced once.")
        if any(price.currency != self.currency for price in self.prices):
            raise ValueError("Every price is in the version's currency.")
        return self

    @property
    def resolved_prices(self) -> list[PriceSpec]:
        """Every price the version is published with. Empty for a free version."""
        if self.prices is not None:
            return list(self.prices)
        if self.price is None or self.price <= 0 or self.interval is None:
            return []
        return [
            PriceSpec(
                billing_interval=self.interval,
                amount=self.price,
                currency=self.currency,
            )
        ]

    @property
    def headline(self) -> PriceSpec | None:
        """The price the version row records as published: monthly if sold monthly.

        The database makes this the version's first `plan_prices` row; the
        others are added beside it. None for a free version.
        """
        prices = self.resolved_prices
        for price in prices:
            if price.billing_interval is BillingInterval.MONTHLY and price.interval_count == 1:
                return price
        return prices[0] if prices else None


class PlanCreate(_Terms):
    code: str = PlanCode
    name: StorableText = Field(min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    is_public: bool = True
    sort_order: int = Field(default=0, ge=0, le=10_000)
    # Who may hold the plan (ADR-113). Omitted, it follows `is_public` exactly
    # as before; `tenant` names the one workspace in `tenant_id`.
    scope: PlanScope | None = None
    tenant_id: uuid.UUID | None = None
    reason: StorableText = Reason

    @model_validator(mode="after")
    def _scope(self) -> Self:
        scope = self.resolved_scope
        if (scope is PlanScope.TENANT) != (self.tenant_id is not None):
            raise ValueError("A tenant plan names its workspace; any other plan names none.")
        if "is_public" in self.model_fields_set and self.is_public != (scope is PlanScope.PUBLIC):
            raise ValueError("is_public must agree with scope: only a public plan is public.")
        return self

    @property
    def resolved_scope(self) -> PlanScope:
        if self.scope is not None:
            return self.scope
        return PlanScope.PUBLIC if self.is_public else PlanScope.PRIVATE


class PlanUpdate(BaseModel):
    """Identity and presentation only. Terms change by publishing a version."""

    model_config = ConfigDict(extra="forbid")

    name: StorableText | None = Field(default=None, min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    is_public: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    expected_revision: int = Field(ge=1)
    reason: StorableText = Reason


class PlanStateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: StorableText = Reason


class PlanVersionCreate(_Terms):
    name: StorableText | None = Field(default=None, min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    reason: StorableText = Reason


class PlanVersionPreviewRequest(_Terms):
    name: StorableText | None = Field(default=None, min_length=1, max_length=100)


class MigrationMode(StrEnum):
    NEXT_RENEWAL = "next_renewal"


class PlanMigrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    mode: MigrationMode = MigrationMode.NEXT_RENEWAL
    reason: StorableText = Reason
    # False returns the preview (how many subscriptions would move) and writes
    # nothing - the default, so a migration is never scheduled by accident.
    confirm: bool = False


class PlanPriceCreate(PriceSpec):
    """A new price for an existing plan version. Platform staff only (ADR-116)."""

    reason: StorableText = Reason


class PlanPriceRetire(BaseModel):
    """Stop offering a price to new customers. Its subscribers keep it."""

    model_config = ConfigDict(extra="forbid")

    reason: StorableText = Reason


class PlanPriceUpdate(BaseModel):
    """What an operator might send to change a price - always refused with 409.

    Every field a price has is accepted, so the refusal is the domain's 409
    naming the correct workflow (retire, then create) rather than a 422 about a
    field name. Nothing here is ever written.
    """

    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    billing_interval: IntervalField | None = None
    interval_count: int | None = Field(default=None, ge=1, le=120)
    currency: StorableText | None = Field(default=None, min_length=3, max_length=3)
    reason: StorableText | None = Field(default=None, max_length=500)


class PriceReferences(BaseModel):
    """What still names a price. Counts only, never whose."""

    subscriptions: int
    scheduled_changes: int
    invoices: int
    offers: int


class PlanPriceRead(BaseModel):
    """One price of a plan version, active or retired (ADR-116)."""

    id: uuid.UUID
    plan_version_id: uuid.UUID
    billing_interval: BillingInterval
    interval_count: int
    amount: str
    currency: str
    active: bool
    created_at: datetime
    created_by: uuid.UUID | None
    reason: str | None
    retired_at: datetime | None
    retired_by: uuid.UUID | None
    retirement_reason: str | None
    references: PriceReferences | None = None

    @classmethod
    def from_model(cls, price: PlanPrice, *, references: dict[str, int] | None = None) -> Self:
        return cls(
            id=price.id,
            plan_version_id=price.plan_version_id,
            billing_interval=price.billing_interval,
            interval_count=price.interval_count,
            amount=f"{price.amount:.2f}",
            currency=price.currency,
            active=price.is_active,
            created_at=price.created_at,
            created_by=price.created_by,
            reason=price.reason,
            retired_at=price.retired_at,
            retired_by=price.retired_by,
            retirement_reason=price.retirement_reason,
            references=PriceReferences(**references) if references is not None else None,
        )


class LimitRead(BaseModel):
    key: LimitKey
    limit: int | None


def limits_of(terms: PlanVersion) -> list[LimitRead]:
    """Every limit a version sets, read as it is enforced (ENT-05 alias included)."""
    return [LimitRead(key=key, limit=term_limit(terms, key)) for key in LimitKey]


class PlanVersionRead(BaseModel):
    """A version's entitlements and every price it has ever been sold at.

    `price`, `currency` and `interval` are the terms the version was published
    with - its first price - kept for earlier clients; `billing_required` and
    `prices` are the full answer (ADR-116). `prices` includes retired rows, each
    marked, so the platform sees the whole price history.
    """

    id: uuid.UUID
    version: int
    name: str
    price: str
    currency: str
    interval: BillingInterval
    billing_required: bool = True
    prices: list[PlanPriceRead] = Field(default_factory=list)
    trial_days: int
    limits: list[LimitRead]
    # The channel types the version allows, as enforced (ENT-09): a version
    # published before ADR-131 states none and allows WhatsApp alone, which
    # `channel_types_stated: false` says.
    allowed_channel_types: list[Channel]
    channel_types_stated: bool
    effective_at: datetime
    created_at: datetime
    created_by: uuid.UUID | None
    reason: str | None
    subscribers: int = 0

    @classmethod
    def from_model(
        cls,
        version: PlanVersion,
        *,
        subscribers: int = 0,
        prices: list[PlanPrice] | None = None,
    ) -> Self:
        return cls(
            id=version.id,
            version=version.version,
            name=version.name,
            price=f"{version.price:.2f}",
            currency=version.currency,
            interval=version.interval,
            billing_required=not version.is_free,
            prices=[PlanPriceRead.from_model(price) for price in prices or []],
            trial_days=version.trial_days,
            limits=limits_of(version),
            allowed_channel_types=ordered(term_channel_types(version)),
            channel_types_stated=not is_legacy(version),
            effective_at=version.effective_at,
            created_at=version.created_at,
            created_by=version.created_by,
            reason=version.reason,
            subscribers=subscribers,
        )


class PlatformPlanRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    scope: PlanScope
    tenant_id: uuid.UUID | None
    is_custom: bool
    is_public: bool
    is_active: bool
    sort_order: int
    revision: int
    current_version: PlanVersionRead | None
    latest_version: PlanVersionRead | None
    subscriber_count: int

    @classmethod
    def build(
        cls,
        plan: Plan,
        *,
        current: PlanVersion | None,
        latest: PlanVersion | None,
        counts: dict[uuid.UUID, int],
        prices: dict[uuid.UUID, list[PlanPrice]] | None = None,
    ) -> Self:
        priced = prices or {}
        return cls(
            id=plan.id,
            code=plan.code,
            name=plan.name,
            description=plan.description,
            scope=plan.scope,
            tenant_id=plan.tenant_id,
            is_custom=plan.is_custom,
            is_public=plan.is_public,
            is_active=plan.is_active,
            sort_order=plan.sort_order,
            revision=plan.revision,
            current_version=(
                PlanVersionRead.from_model(
                    current,
                    subscribers=counts.get(current.id, 0),
                    prices=priced.get(current.id, []),
                )
                if current is not None
                else None
            ),
            latest_version=(
                PlanVersionRead.from_model(
                    latest,
                    subscribers=counts.get(latest.id, 0),
                    prices=priced.get(latest.id, []),
                )
                if latest is not None
                else None
            ),
            subscriber_count=sum(counts.values()),
        )


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class FeatureRead(BaseModel):
    """One thing a plan can set, and how Wasla enforces it.

    `key` is a `LimitKey` value, `allowed_channel_types` (a set, not a number:
    `kind: channel_policy`), or the retired `whatsapp_numbers`
    (`kind: retired`), listed so an operator reading an old version knows
    what replaced it.
    """

    key: str
    description: str
    unit: str
    kind: Literal["hard_limit", "meter_only", "account_limit", "channel_policy", "retired"]
    enforcement: str
    concurrency_safe: bool
    unlimited: str = "null"
    replaced_by: str | None = None
    # Whether a top-up product, a grant or a custom plan's top-up can raise
    # this key (PLAT-G5): the seven sellable top-up keys, never the retired one.
    topup_eligible: bool = False


class LimitChange(BaseModel):
    key: LimitKey
    old: int | None
    new: int | None
    workspaces_above_new_limit: int | None


class ChannelTypesChange(BaseModel):
    """What publishing these channel types would change (ENT-09).

    `workspaces_holding_a_removed_type` counts serving subscribers of the plan
    holding an active connection of a type the proposal no longer allows -
    the workspaces a migration onto it would put over their allowed types.
    """

    old: list[Channel] | None
    new: list[Channel]
    removed: list[Channel]
    added: list[Channel]
    workspaces_holding_a_removed_type: int


class PlanVersionPreview(BaseModel):
    plan_id: uuid.UUID
    current_version: int | None
    proposed_price: str
    current_price: str | None
    currency: str
    interval: BillingInterval
    active_subscriptions: int
    subscriptions_on_current_version: int
    subscriptions_staying_on_old_versions: int
    subscriptions_scheduled_for_migration: int
    limits: list[LimitChange]
    channel_types: ChannelTypesChange
    note: str


class MigrationRead(BaseModel):
    id: uuid.UUID | None
    plan_id: uuid.UUID
    from_version: int
    to_version: int
    reason: str
    affected_subscriptions: int
    scheduled: bool
    created_at: datetime | None

    @classmethod
    def build(
        cls,
        migration: PlanVersionMigration | None,
        *,
        plan_id: uuid.UUID,
        from_version: int,
        to_version: int,
        reason: str,
        affected: int,
    ) -> Self:
        return cls(
            id=migration.id if migration is not None else None,
            plan_id=plan_id,
            from_version=from_version,
            to_version=to_version,
            reason=reason,
            affected_subscriptions=affected,
            scheduled=migration is not None,
            created_at=migration.created_at if migration is not None else None,
        )


# ------------------------------------------------------------- subscriptions


class ChangeMode(StrEnum):
    NOW = "now"
    NEXT_RENEWAL = "next_renewal"


class FinancialBasis(StrEnum):
    """What pays for a priced plan an operator applies *now*.

    There is deliberately no "just grant it". A priced plan held without money
    is either a complimentary grant, said out loud and recorded, or covered by
    a manual payment the operator has seen.
    """

    COMPLIMENTARY = "complimentary"
    MANUAL_PAYMENT = "manual_payment"


class ManualPaymentDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    method: StorableText = Field(min_length=1, max_length=50)
    reference: StorableText | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None


class SubscriptionChangePlan(BaseModel):
    """Move a subscriber to a version at one exact price (ADR-116).

    `plan_price_id` names the price - and so the billing term - the subscriber
    renews at; it must be a price of `plan_version_id`. Omitted, the version's
    default (monthly) price is used when it has one; a version sold only
    yearly must be named explicitly. Ignored for a free version.
    """

    model_config = ConfigDict(extra="forbid")

    plan_version_id: uuid.UUID
    plan_price_id: uuid.UUID | None = None
    mode: ChangeMode
    financial_basis: FinancialBasis | None = None
    manual_payment: ManualPaymentDetails | None = None
    complimentary_until: datetime | None = None
    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)


class SubscriptionCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    immediately: bool = False
    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)


class SubscriptionResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)


class PlatformSubscriptionRead(BaseModel):
    """A subscription as support sees it: the paid term and the usage cycle apart.

    `current_period_*` is the **billing term** the customer has paid for - a
    year on a yearly price - and `usage_period_*` the **monthly usage cycle**
    their allowances are counted over (ADR-116). `billing_interval` and
    `amount` are the price the subscription renews at.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str | None
    plan_version_id: uuid.UUID | None
    plan_version: int | None
    plan_price_id: uuid.UUID | None = None
    billing_interval: BillingInterval | None = None
    interval_count: int | None = None
    amount: str | None = None
    currency: str | None = None
    status: SubscriptionStatus
    current_period_start: datetime
    current_period_end: datetime
    usage_period_start: datetime | None = None
    usage_period_end: datetime | None = None
    billing_anchor_at: datetime | None
    cancel_at_period_end: bool
    cancelled_at: datetime | None
    ended_at: datetime | None
    scheduled_plan_version_id: uuid.UUID | None
    scheduled_plan_price_id: uuid.UUID | None = None
    scheduled_change_source: ScheduledChangeSource | None
    revision: int

    @classmethod
    def build(
        cls,
        subscription: Subscription,
        *,
        plan_code: str | None,
        version: int | None,
        price: PlanPrice | None = None,
    ) -> Self:
        return cls(
            id=subscription.id,
            tenant_id=subscription.tenant_id,
            plan_id=subscription.plan_id,
            plan_code=plan_code,
            plan_version_id=subscription.plan_version_id,
            plan_version=version,
            plan_price_id=subscription.plan_price_id,
            billing_interval=price.billing_interval if price is not None else None,
            interval_count=price.interval_count if price is not None else None,
            amount=f"{price.amount:.2f}" if price is not None else None,
            currency=price.currency if price is not None else None,
            status=subscription.status,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            usage_period_start=subscription.usage_period_start,
            usage_period_end=subscription.usage_period_end,
            billing_anchor_at=subscription.billing_anchor_at,
            cancel_at_period_end=subscription.cancel_at_period_end,
            cancelled_at=subscription.cancelled_at,
            ended_at=subscription.ended_at,
            scheduled_plan_version_id=subscription.scheduled_plan_version_id,
            scheduled_plan_price_id=subscription.scheduled_plan_price_id,
            scheduled_change_source=subscription.scheduled_change_source,
            revision=subscription.revision,
        )


class TimelineEntry(BaseModel):
    occurred_at: datetime
    kind: Literal["audit", "invoice", "payment"]
    action: str
    actor: str | None
    target_id: uuid.UUID | None
    detail: dict[str, Any] | None


# ---------------------------------------------------------------- invoices


class PlatformInvoiceRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    subscription_id: uuid.UUID | None
    purpose: InvoicePurpose
    status: InvoiceStatus
    plan_code: str
    plan_version_id: uuid.UUID | None
    # The price and billing term this invoice covers, copied at issue (ADR-116).
    plan_price_id: uuid.UUID | None = None
    billing_interval: BillingInterval | None = None
    interval_count: int | None = None
    amount_due: str
    amount_paid: str
    outstanding: str
    currency: str
    period_start: datetime
    period_end: datetime
    issued_at: datetime | None
    paid_at: datetime | None
    voided_at: datetime | None
    collection_attempts: int
    next_collection_at: datetime | None
    notes: str | None
    lines: list[dict[str, Any]]
    revision: int

    @classmethod
    def from_model(cls, invoice: Invoice) -> Self:
        return cls(
            id=invoice.id,
            tenant_id=invoice.tenant_id,
            subscription_id=invoice.subscription_id,
            purpose=invoice.purpose,
            status=invoice.status,
            plan_code=invoice.plan_code,
            plan_version_id=invoice.plan_version_id,
            plan_price_id=invoice.plan_price_id,
            billing_interval=invoice.billing_interval,
            interval_count=invoice.interval_count,
            amount_due=f"{invoice.amount_due:.2f}",
            amount_paid=f"{invoice.amount_paid:.2f}",
            outstanding=f"{invoice.outstanding:.2f}",
            currency=invoice.currency,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            issued_at=invoice.issued_at,
            paid_at=invoice.paid_at,
            voided_at=invoice.voided_at,
            collection_attempts=invoice.collection_attempts,
            next_collection_at=invoice.next_collection_at,
            notes=invoice.notes,
            lines=list(invoice.lines or []),
            revision=invoice.revision,
        )


class ManualPaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    method: StorableText = Field(min_length=1, max_length=50)
    reference: StorableText | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)
    # The deliberate recovery of an invoice written off as uncollectible.
    recover_uncollectible: bool = False


class InvoiceVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)
    # Required when the workspace is behind on this renewal - see
    # `InvoiceSettlement.void`.
    subscription_policy: Literal["unchanged", "cancel", "waive"] | None = None


# ---------------------------------------------------------------- payments


class PlatformPaymentRead(BaseModel):
    """A payment as an operator sees it: provider identifiers, never secrets."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    invoice_id: uuid.UUID
    status: PaymentStatus
    amount: str
    refunded_amount: str
    currency: str
    provider: str
    provider_mode: str | None
    provider_transaction_id: str | None
    provider_order_id: str | None
    provider_intention_id: str | None
    provider_integration_id: str | None
    # An operator's own reference for money that arrived outside a processor.
    manual_reference: str | None
    # Whether this payment's money counts towards its invoice; a collected
    # payment that does not is held with an incident (DB-001).
    applied: bool
    is_automatic: bool
    collection_state: str | None
    failure_reason: str | None
    refund_pending: bool
    refund_requested_total: str | None
    refunded_at: datetime | None
    processed_at: datetime | None
    created_at: datetime
    revision: int

    @classmethod
    def from_model(cls, payment: Payment) -> Self:
        return cls(
            id=payment.id,
            tenant_id=payment.tenant_id,
            invoice_id=payment.invoice_id,
            status=payment.status,
            amount=f"{payment.amount:.2f}",
            refunded_amount=f"{payment.refunded_amount:.2f}",
            currency=payment.currency,
            provider=payment.provider,
            provider_mode=payment.provider_mode,
            provider_transaction_id=payment.provider_reference,
            provider_order_id=payment.provider_order_id,
            provider_intention_id=payment.provider_intent_reference,
            provider_integration_id=payment.provider_integration_id,
            manual_reference=payment.manual_reference,
            applied=payment.applied_at is not None,
            is_automatic=payment.is_automatic,
            collection_state=payment.collection_state.value if payment.collection_state else None,
            failure_reason=payment.failure_reason,
            refund_pending=payment.refund_requested_amount is not None,
            refund_requested_total=_money(payment.refund_requested_amount),
            refunded_at=payment.refunded_at,
            processed_at=payment.processed_at,
            created_at=payment.created_at,
            revision=payment.revision,
        )


class PlatformRefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)


# ---------------------------------------------------------- reconciliation


class IncidentRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    kind: BillingIncidentKind
    status: BillingIncidentStatus
    payment_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    provider: str | None
    provider_transaction_id: str | None
    amount: str | None
    currency: str | None
    detail: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None

    @classmethod
    def from_model(cls, incident: BillingIncident) -> Self:
        return cls(
            id=incident.id,
            tenant_id=incident.tenant_id,
            kind=incident.kind,
            status=incident.status,
            payment_id=incident.payment_id,
            invoice_id=incident.invoice_id,
            provider=incident.provider,
            provider_transaction_id=incident.provider_transaction_id,
            amount=_money(incident.amount),
            currency=incident.currency,
            detail=incident.detail,
            created_at=incident.created_at,
            resolved_at=incident.resolved_at,
            resolution_note=incident.resolution_note,
        )


class IncidentResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: StorableText = Reason


class ReconciliationCategory(BaseModel):
    category: str
    count: int
    description: str


class ReconciliationView(BaseModel):
    generated_at: datetime
    categories: list[ReconciliationCategory]
    pending_hosted_payments: list[PlatformPaymentRead]
    unresolved_automatic_attempts: list[PlatformPaymentRead]
    open_incidents: list[IncidentRead]


class ReconciliationRunResult(BaseModel):
    payment_id: uuid.UUID
    verdict: str
    payment: PlatformPaymentRead | None
