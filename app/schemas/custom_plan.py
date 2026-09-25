"""Custom plans: one workspace's own plan, written by platform staff (ADR-113).

A custom plan is an ordinary `Plan` with `scope = tenant` and ordinary immutable
`PlanVersion`s - nothing here is a second plan model. These contracts serve the
one-screen operator flow ("Company -> Billing -> Create Custom Plan"): preview,
then create and optionally assign.

**Every one of the seven limits is required.** `null` means unlimited and `0`
means none - both are deliberate choices, so neither is allowed to happen by
leaving a field out. Storage is integer bytes (1 GiB = 1024**3); a client that
shows GiB converts before sending.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.billing import (
    MAX_LIMIT_VALUE,
    MAX_PLAN_PRICE,
    SUPPORTED_CURRENCIES,
    BillingInterval,
    LimitKey,
    PlanScope,
)
from app.db.models.enums import TenantStatus
from app.schemas.platform_billing import (
    FinancialBasis,
    ManualPaymentDetails,
    PlanVersionRead,
    PlatformPlanRead,
    PlatformSubscriptionRead,
)
from app.schemas.text import StorableText

#: The seven keys a custom plan is written in, in the order the form shows them.
CUSTOM_PLAN_KEYS: tuple[LimitKey, ...] = (
    LimitKey.PERIOD_MESSAGES,
    LimitKey.PERIOD_AI_TURNS,
    LimitKey.PERIOD_CAMPAIGN_MESSAGES,
    LimitKey.STORAGE_BYTES,
    LimitKey.WHATSAPP_NUMBERS,
    LimitKey.TEAM_MEMBERS,
    LimitKey.KNOWLEDGE_DOCUMENTS,
)

Limit = Field(ge=0, le=MAX_LIMIT_VALUE)


class AssignmentMode(StrEnum):
    NOW = "now"
    NEXT_RENEWAL = "next_renewal"


class CustomPlanBasis(StrEnum):
    """What pays for a *priced* custom plan applied now. Never "just grant it".

    ``complimentary`` and ``manual_payment`` are the operator bases the
    subscription change already knows. ``customer_checkout`` assigns nothing:
    it makes an **offer** (ADR-114) that the workspace's owner sees with its
    full terms and accepts and pays at a hosted checkout, after which
    settlement applies exactly the offered version.
    """

    COMPLIMENTARY = "complimentary"
    MANUAL_PAYMENT = "manual_payment"
    CUSTOMER_CHECKOUT = "customer_checkout"


class CustomPlanTerms(BaseModel):
    """The commercial terms of a custom plan's version, as the form sends them."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: StorableText = Field(min_length=1, max_length=100)
    description: StorableText | None = Field(default=None, max_length=2000)
    price: Decimal = Field(ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    billing_interval: BillingInterval

    period_messages: int | None = Limit
    period_ai_turns: int | None = Limit
    period_campaign_messages: int | None = Limit
    storage_bytes: int | None = Limit
    whatsapp_numbers: int | None = Limit
    team_members: int | None = Limit
    knowledge_documents: int | None = Limit

    effective_at: datetime | None = None
    assignment_mode: AssignmentMode = AssignmentMode.NEXT_RENEWAL

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        upper = value.upper()
        if upper not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Only {', '.join(sorted(SUPPORTED_CURRENCIES))} is supported.")
        return upper

    def limits(self) -> dict[str, int | None]:
        """The seven limits, keyed as a plan version stores them."""
        return {key.value: getattr(self, key.value) for key in CUSTOM_PLAN_KEYS}


class CustomPlanPreviewRequest(CustomPlanTerms):
    """What creating these terms would mean for the workspace. Writes nothing."""


class CustomPlanCreate(CustomPlanTerms):
    """Create the plan and its version 1, and optionally put the workspace on it."""

    assign_to_tenant: bool = False
    financial_basis: CustomPlanBasis | None = None
    manual_payment: ManualPaymentDetails | None = None
    complimentary_until: datetime | None = None
    # With `financial_basis: customer_checkout`: when the offer stops being
    # acceptable. Null leaves it open until accepted, declined or withdrawn.
    offer_expires_at: datetime | None = None
    # Required when assigning: the subscription revision the operator saw.
    expected_subscription_revision: int | None = Field(default=None, ge=1)
    reason: StorableText = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def _assignment(self) -> Self:
        if self.financial_basis is CustomPlanBasis.CUSTOMER_CHECKOUT:
            # An offer: nothing is assigned, so no subscription revision is
            # needed and the assignment mode does not apply (ADR-114).
            return self
        if self.offer_expires_at is not None:
            raise ValueError("offer_expires_at applies only to financial_basis customer_checkout.")
        if not self.assign_to_tenant:
            return self
        if self.expected_subscription_revision is None:
            raise ValueError("Assigning needs expected_subscription_revision.")
        if self.assignment_mode is AssignmentMode.NOW and self.price > 0:
            if self.financial_basis is None:
                raise ValueError(
                    "A priced plan applied now needs a financial_basis: customer_checkout, "
                    "manual_payment or complimentary. Nothing is granted for free."
                )
            if (
                self.financial_basis is CustomPlanBasis.MANUAL_PAYMENT
                and self.manual_payment is None
            ):
                raise ValueError("A manual payment needs its details.")
        return self

    @property
    def operator_basis(self) -> FinancialBasis | None:
        if self.financial_basis is CustomPlanBasis.COMPLIMENTARY:
            return FinancialBasis.COMPLIMENTARY
        if self.financial_basis is CustomPlanBasis.MANUAL_PAYMENT:
            return FinancialBasis.MANUAL_PAYMENT
        return None


class CustomPlanTenantRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus


class CurrentPlanRead(BaseModel):
    plan_id: uuid.UUID
    code: str
    name: str
    scope: PlanScope
    version: int
    price: str
    currency: str
    interval: BillingInterval


class LimitComparison(BaseModel):
    """One key, now and as proposed, against what the workspace uses."""

    key: LimitKey
    kind: Literal["usage", "capacity"]
    current: int | None
    proposed: int | None
    # proposed - current, when both are finite. Null when either is unlimited.
    difference: int | None
    used: int
    # Live top-ups and platform grants, which stay until their own expiry
    # whatever the plan becomes.
    active_topups: int
    proposed_effective: int | None
    # The proposal is already below what is in use. Nothing is deleted if it
    # applies; the workspace is simply over its limit until usage fits.
    below_current_usage: bool


class EstimatedCharge(BaseModel):
    amount: str
    currency: str
    due_at: datetime | None
    basis: Literal["checkout", "manual_payment", "complimentary", "renewal", "none"]


class CustomPlanPreview(BaseModel):
    tenant: CustomPlanTenantRead
    current_plan: CurrentPlanRead | None
    proposed_code: str
    proposed_name: str
    proposed_price: str
    currency: str
    interval: BillingInterval
    limits: list[LimitComparison]
    # Limits a custom plan does not set (agents, owned workspaces), copied from
    # the plan the workspace holds so a custom plan never silently makes them
    # unlimited.
    inherited_limits: dict[str, int | None]
    effective_mode: AssignmentMode
    effective_at: datetime | None
    estimated_next_charge: EstimatedCharge | None
    warnings: list[str]


class CustomPlanAssignment(BaseModel):
    mode: AssignmentMode | None
    status: Literal[
        "assigned", "scheduled", "awaiting_customer_checkout", "offered", "not_assigned"
    ]
    subscription: PlatformSubscriptionRead | None


class CustomPlanResult(BaseModel):
    plan: PlatformPlanRead
    version: PlanVersionRead
    assignment: CustomPlanAssignment
    preview: CustomPlanPreview
    # Set when the basis was customer checkout: the plan reaches the workspace
    # only when its owner accepts and pays this offer (ADR-114).
    offer: CustomPlanOfferRead | None = None


# ------------------------------------------------------------ offers (ADR-114)


class OfferLimitRead(BaseModel):
    """One of the seven limits an offer carries. Null is unlimited."""

    key: LimitKey
    kind: Literal["usage", "capacity"]
    limit: int | None


class OfferPeriodRead(BaseModel):
    """When the offered terms would be in force if paid now.

    A paid period starts when the payment settles (BILL-03), so this is an
    illustration computed at read time, not a promise about a fixed date.
    """

    starts: Literal["on_payment"] = "on_payment"
    interval: BillingInterval
    if_paid_now_start: datetime
    if_paid_now_end: datetime


class CustomPlanOfferRead(BaseModel):
    """Everything a customer must see before paying: price, terms and period."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    plan_id: uuid.UUID
    plan_code: str
    plan_version_id: uuid.UUID
    version: int
    name: str
    description: str | None
    price: str
    currency: str
    interval: BillingInterval
    limits: list[OfferLimitRead]
    # Limits the custom plan does not set (agents, owned workspaces), as the
    # version holds them.
    other_limits: dict[str, int | None]
    effective_period: OfferPeriodRead
    expires_at: datetime | None
    created_at: datetime
    accepted_at: datetime | None
    activated_at: datetime | None
    declined_at: datetime | None
    decline_reason: str | None
    cancelled_at: datetime | None
    expired_at: datetime | None
    can_accept: bool
    # Payment method guidance (spec: no forced card saving).
    renewal_note: str
    revision: int


class CustomPlanOfferCreate(BaseModel):
    """Offer one version of a workspace's own custom plan to that workspace."""

    model_config = ConfigDict(extra="forbid")

    plan_version_id: uuid.UUID
    expires_at: datetime | None = None
    reason: StorableText = Field(min_length=3, max_length=500)


class CustomPlanOfferCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StorableText = Field(min_length=3, max_length=500)
    expected_revision: int = Field(ge=1)


class CustomPlanOfferAccept(BaseModel):
    """Accept and pay. Names nothing that could price the purchase."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: StorableText | None = Field(default=None, min_length=1, max_length=100)


class CustomPlanOfferDecline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StorableText | None = Field(default=None, max_length=500)


class CustomPlanOfferCheckoutStarted(BaseModel):
    """Where to pay. The client secret travels only inside `redirect_url`."""

    offer_id: uuid.UUID
    redirect_url: str
    invoice_id: uuid.UUID
    payment_id: uuid.UUID
    amount: str
    currency: str
    plan_version_id: uuid.UUID


# `CustomPlanResult.offer` names a model declared after it.
CustomPlanResult.model_rebuild()
