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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.billing import (
    MAX_LIMIT_VALUE,
    MAX_PLAN_PRICE,
    METER_ONLY_LIMITS,
    SUPPORTED_CURRENCIES,
)
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
from app.schemas.text import StorableText

ProductCode = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
Reason = Field(min_length=3, max_length=500)
Quantity = Field(gt=0, le=MAX_LIMIT_VALUE)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _currency(value: str) -> str:
    upper = value.upper()
    if upper not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Only {', '.join(sorted(SUPPORTED_CURRENCIES))} is supported.")
    return upper


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
    price: str
    currency: str
    scope: TopupScope
    validity_policy: TopupValidity

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
            price=_money(product.price),
            currency=product.currency,
            scope=product.scope,
            validity_policy=product.validity_policy,
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

    @classmethod
    def build(cls, product: TopupProduct, *, purchases: int) -> Self:
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
    reason: StorableText = Reason

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        return _currency(value)

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if (self.scope is TopupScope.TENANT) != (self.tenant_id is not None):
            raise ValueError("A tenant top-up names its workspace; a global one names none.")
        return self


class TopupProductUpdate(BaseModel):
    """Presentation and commercial terms for *new* purchases.

    Existing purchases keep the terms they were bought at; the entitlement,
    scope and owner cannot change - that is a different product.
    """

    model_config = ConfigDict(extra="forbid")

    name: StorableText | None = Field(default=None, min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    quantity: int | None = Field(default=None, gt=0, le=MAX_LIMIT_VALUE)
    price: Decimal | None = Field(
        default=None, ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2
    )
    is_public: bool | None = None
    expected_revision: int = Field(ge=1)
    reason: StorableText = Reason


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
    # One rule in v1: until the end of the subscription's current period.
    valid_until: Literal["current_period_end"] = "current_period_end"
    reason: StorableText = Reason
    expected_subscription_revision: int = Field(ge=1)


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


class TenantBillingSummary(BaseModel):
    """Billing -> Usage & Top-ups, in one request, for a workspace owner."""

    subscription: SubscriptionRead | None
    entitlements: list[EntitlementRead]
    active_topups: list[TopupPurchaseRead]
    recent_purchases: list[TopupPurchaseRead]
    topups_available: int
