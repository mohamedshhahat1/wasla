"""Top-up contracts, for workspaces and for platform staff (ADR-113).

The customer side names a product id and, optionally, an idempotency key -
nothing that could set a price, a quantity or a workspace. The platform side
carries a reason on every mutation and the revision it was based on on every
change to an existing row, like the rest of `/platform/billing`.

No response here carries a card token, a provider secret or a payment key.
Amounts are strings with two decimals; storage quantities are integer bytes
(1 GiB = 1024**3 bytes), never floating gigabytes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.billing import (
    MAX_LIMIT_VALUE,
    MAX_PLAN_PRICE,
    METER_ONLY_LIMITS,
    SUPPORTED_CURRENCIES,
    LimitKey,
)
from app.db.models.channel import Channel
from app.db.models.invoice import PaymentStatus
from app.db.models.topup import (
    TopupEntitlement,
    TopupProduct,
    TopupPurchase,
    TopupScope,
    TopupSource,
    TopupStatus,
    TopupValidity,
)
from app.schemas.billing import EntitlementRead, SubscriptionRead
from app.schemas.custom_plan import CustomPlanOfferRead
from app.schemas.text import StorableText

ProductCode = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
Reason = Field(min_length=3, max_length=500)
Quantity = Field(gt=0, le=MAX_LIMIT_VALUE)
EligiblePlanCode = Annotated[
    str, Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
]
# How many plans one product may name. A guard on the request, not a product
# rule: a catalogue has a handful of plans.
MAX_ELIGIBLE_PLANS = 100


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _currency(value: str) -> str:
    upper = value.upper()
    if upper not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Only {', '.join(sorted(SUPPORTED_CURRENCIES))} is supported.")
    return upper


def _sellable(entitlement: TopupEntitlement) -> TopupEntitlement:
    """Refuse the retired number key by name: it is sold as channel slots now (ENT-05)."""
    if entitlement.is_retired:
        raise ValueError(
            "whatsapp_numbers is retired; sell channel_connections instead "
            "(typed whatsapp for a WhatsApp-only slot)."
        )
    return entitlement


def _typed(entitlement: TopupEntitlement, channel_type: Channel | None) -> None:
    """A slot is typed for a channel only on `channel_connections` (ENT-11)."""
    if channel_type is not None and entitlement.limit_key is not LimitKey.CHANNEL_CONNECTIONS:
        raise ValueError("Only a channel_connections top-up is typed for a channel.")


def _distinct(codes: list[str] | None) -> list[str] | None:
    if codes is None:
        return None
    lowered = [code.strip().lower() for code in codes]
    if len(set(lowered)) != len(lowered):
        raise ValueError("Each eligible plan is named once.")
    return lowered


# ------------------------------------------------------------------ products


class TopupProductRead(BaseModel):
    """A product as a workspace's catalogue shows it."""

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    entitlement_key: TopupEntitlement
    kind: Literal["usage", "capacity"]
    # False only for `period_messages`: the allowance it raises is measured,
    # never enforced (ADR-030). Said here so a client can say so too.
    enforced: bool
    quantity: int
    # Null only on a product staff have not priced yet, which is inactive and
    # so never in a workspace's catalogue (ENT-20).
    price: str | None
    currency: str
    scope: TopupScope
    validity_policy: TopupValidity
    # A channel slot only one channel type may use (ENT-11); null is a general
    # slot any allowed channel may use, and on every other key.
    channel_type: Channel | None = None

    @classmethod
    def from_model(cls, product: TopupProduct) -> Self:
        return cls(
            id=product.id,
            code=product.code,
            name=product.name,
            description=product.description,
            entitlement_key=product.entitlement_key,
            kind="capacity" if product.entitlement_key.is_capacity else "usage",
            enforced=product.entitlement_key.limit_key not in METER_ONLY_LIMITS,
            quantity=product.quantity,
            price=_money(product.price) if product.price is not None else None,
            currency=product.currency,
            scope=product.scope,
            validity_policy=product.validity_policy,
            channel_type=product.channel_type,
        )


class PlatformTopupProductRead(TopupProductRead):
    """A product as platform staff see it: visibility, owner, revision, use."""

    tenant_id: uuid.UUID | None
    is_active: bool
    is_public: bool
    revision: int
    purchase_count: int
    created_at: datetime
    updated_at: datetime
    # The plans this product is offered to (ENT-13); empty means every plan.
    eligible_plan_codes: list[str] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        product: TopupProduct,
        *,
        purchases: int,
        eligible_plan_codes: list[str] | None = None,
    ) -> Self:
        base = TopupProductRead.from_model(product).model_dump()
        return cls(
            **base,
            tenant_id=product.tenant_id,
            is_active=product.is_active,
            is_public=product.is_public,
            revision=product.revision,
            purchase_count=purchases,
            created_at=product.created_at,
            updated_at=product.updated_at,
            eligible_plan_codes=eligible_plan_codes or [],
        )


class TopupProductCreate(BaseModel):
    """A new product. Its entitlement, scope and owner are permanent."""

    model_config = ConfigDict(extra="forbid")

    code: str = ProductCode
    name: StorableText = Field(min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    entitlement_key: TopupEntitlement
    quantity: int = Quantity
    price: Decimal = Field(ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    scope: TopupScope = TopupScope.GLOBAL
    tenant_id: uuid.UUID | None = None
    is_public: bool = True
    # A slot only this channel type may use (ENT-11); `channel_connections`
    # only. Null is a general slot.
    channel_type: Channel | None = None
    # The plans offered it (ENT-13). Null or empty: every plan.
    eligible_plan_codes: list[EligiblePlanCode] | None = Field(
        default=None, max_length=MAX_ELIGIBLE_PLANS
    )
    reason: StorableText = Reason

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        return _currency(value)

    @field_validator("entitlement_key")
    @classmethod
    def _not_retired(cls, value: TopupEntitlement) -> TopupEntitlement:
        return _sellable(value)

    @field_validator("eligible_plan_codes")
    @classmethod
    def _each_once(cls, value: list[str] | None) -> list[str] | None:
        return _distinct(value)

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if (self.scope is TopupScope.TENANT) != (self.tenant_id is not None):
            raise ValueError("A tenant top-up names its workspace; a global one names none.")
        _typed(self.entitlement_key, self.channel_type)
        return self


class TopupProductUpdate(BaseModel):
    """Presentation and commercial terms for *new* purchases.

    Existing purchases keep the terms they were bought at; the entitlement,
    scope and owner cannot change - that is a different product.

    `channel_type` and `eligible_plan_codes` change only when named: a
    `channel_type` of null turns a typed slot general (ENT-11), and an
    `eligible_plan_codes` of null or [] offers the product to every plan
    (ENT-13).
    """

    model_config = ConfigDict(extra="forbid")

    name: StorableText | None = Field(default=None, min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    quantity: int | None = Field(default=None, gt=0, le=MAX_LIMIT_VALUE)
    price: Decimal | None = Field(
        default=None, ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2
    )
    is_public: bool | None = None
    channel_type: Channel | None = None
    eligible_plan_codes: list[EligiblePlanCode] | None = Field(
        default=None, max_length=MAX_ELIGIBLE_PLANS
    )
    expected_revision: int = Field(ge=1)
    reason: StorableText = Reason

    @field_validator("eligible_plan_codes")
    @classmethod
    def _each_once(cls, value: list[str] | None) -> list[str] | None:
        return _distinct(value)


class TopupStateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: StorableText = Reason


# ----------------------------------------------------------------- purchases


class TopupCheckoutRequest(BaseModel):
    """Buying a product. There is deliberately nothing here to price it with."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: StorableText | None = Field(default=None, min_length=1, max_length=100)


class TopupCheckoutStarted(BaseModel):
    """Where to send the customer, and exactly what the page sells."""

    redirect_url: str
    purchase_id: uuid.UUID
    invoice_id: uuid.UUID
    payment_id: uuid.UUID
    amount: str
    currency: str
    entitlement_key: TopupEntitlement
    quantity: int
    expires_at: datetime
    channel_type: Channel | None = None


class TopupPurchaseRead(BaseModel):
    """A top-up a workspace holds or held, as frozen when it was bought."""

    id: uuid.UUID
    product_id: uuid.UUID | None
    product_code: str | None
    product_name: str
    source: TopupSource
    entitlement_key: TopupEntitlement
    quantity: int
    unit_price: str
    amount: str
    currency: str
    status: TopupStatus
    purchased_at: datetime
    paid_at: datetime | None
    granted_at: datetime | None
    expires_at: datetime
    billing_period_start: datetime
    billing_period_end: datetime
    invoice_id: uuid.UUID | None
    payment_id: uuid.UUID | None
    payment_status: PaymentStatus | None = None
    # As frozen at purchase: a typed channel slot's channel (ENT-11).
    channel_type: Channel | None = None

    @classmethod
    def from_model(
        cls, purchase: TopupPurchase, *, payment_status: PaymentStatus | None = None
    ) -> Self:
        return cls(
            id=purchase.id,
            product_id=purchase.topup_product_id,
            product_code=purchase.product_code,
            product_name=purchase.product_name,
            source=purchase.source,
            entitlement_key=purchase.entitlement_key,
            channel_type=purchase.channel_type,
            quantity=purchase.quantity,
            unit_price=_money(purchase.unit_price),
            amount=_money(purchase.total_amount),
            currency=purchase.currency,
            status=purchase.status,
            purchased_at=purchase.created_at,
            paid_at=purchase.paid_at,
            granted_at=purchase.granted_at,
            expires_at=purchase.expires_at,
            billing_period_start=purchase.billing_period_start,
            billing_period_end=purchase.billing_period_end,
            invoice_id=purchase.invoice_id,
            payment_id=purchase.payment_id,
            payment_status=payment_status,
        )


class PlatformTopupPurchaseRead(TopupPurchaseRead):
    """A purchase as platform staff see it: whose, and who granted it."""

    tenant_id: uuid.UUID
    reason: str | None
    actor_id: uuid.UUID | None
    revision: int

    @classmethod
    def build(cls, purchase: TopupPurchase, *, payment_status: PaymentStatus | None = None) -> Self:
        base = TopupPurchaseRead.from_model(purchase, payment_status=payment_status).model_dump()
        return cls(
            **base,
            tenant_id=purchase.tenant_id,
            reason=purchase.reason,
            actor_id=purchase.actor_id,
            revision=purchase.revision,
        )


class TopupPurchasePage(BaseModel):
    items: list[TopupPurchaseRead]
    total: int
    limit: int
    offset: int


class TopupGrantCreate(BaseModel):
    """An operator's complimentary allowance. Never a payment (ADR-113)."""

    model_config = ConfigDict(extra="forbid")

    entitlement_key: TopupEntitlement
    quantity: int = Quantity
    # A typed channel slot (ENT-11), `channel_connections` only; it must be a
    # channel the workspace's plan includes (ENT-12).
    channel_type: Channel | None = None
    # One rule in v1: until the end of the subscription's current period.
    valid_until: Literal["current_period_end"] = "current_period_end"
    reason: StorableText = Reason
    expected_subscription_revision: int = Field(ge=1)

    @field_validator("entitlement_key")
    @classmethod
    def _not_retired(cls, value: TopupEntitlement) -> TopupEntitlement:
        return _sellable(value)

    @model_validator(mode="after")
    def _channel(self) -> Self:
        _typed(self.entitlement_key, self.channel_type)
        return self


class TopupRefundReview(BaseModel):
    """What happens to a granted top-up whose money went back.

    ``keep`` leaves the allowance in place (goodwill). ``withdraw`` removes it
    from the effective limit from now on - never deleting anything a capacity
    held, never making usage negative.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep", "withdraw"]
    reason: StorableText = Reason
    expected_revision: int = Field(ge=1)


# ------------------------------------------------------------------ summary


class PaymentRequiredRead(BaseModel):
    """A renewal the customer must pay themselves ("Payment required").

    Shown when no saved card collected it - none was saved, or the charge was
    declined. Paid with `POST /billing/checkout {"invoice_id": ...}`; the
    ordinary grace and dunning rules apply meanwhile.
    """

    invoice_id: uuid.UUID
    plan_code: str
    amount_due: str
    currency: str
    period_start: datetime
    period_end: datetime
    issued_at: datetime | None
    pay_with: str = "POST /billing/checkout"


class TenantBillingSummary(BaseModel):
    """Billing -> Usage & Top-ups, in one request, for a workspace owner."""

    subscription: SubscriptionRead | None
    entitlements: list[EntitlementRead]
    active_topups: list[TopupPurchaseRead]
    recent_purchases: list[TopupPurchaseRead]
    topups_available: int
    # ADR-114: the custom plan offer awaiting the owner, and renewals the
    # owner must pay by hand. Defaulted so older clients keep parsing.
    open_offer: CustomPlanOfferRead | None = None
    payment_required: list[PaymentRequiredRead] = Field(default_factory=list)
    # Whether renewals will be charged to a saved card automatically.
    automatic_renewal: bool = False
