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
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.billing import (
    MAX_LIMIT_VALUE,
    MAX_PLAN_PRICE,
    SUPPORTED_CURRENCIES,
    BillingInterval,
    LimitKey,
    Plan,
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
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.schemas.text import StorableText

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
    """Known keys only; `null` is unlimited; zero is zero; negative is refused."""
    known = {key.value for key in LimitKey}
    for key, limit in value.items():
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


class _Terms(BaseModel):
    """Commercial terms, validated identically wherever they are written."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=0, le=MAX_PLAN_PRICE, max_digits=12, decimal_places=2)
    currency: StorableText = Field(min_length=3, max_length=3)
    interval: BillingInterval
    # Keyed by entitlement key. `null` (or an absent key) is unlimited.
    limits: dict[str, int | None] = Field(default_factory=dict, json_schema_extra=LIMITS_SCHEMA)
    # Trials are not implemented: a free plan never needs one and a priced
    # plan's trial never applied (BILL-23). Zero is the only accepted value.
    trial_days: Literal[0] = 0
    effective_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        upper = value.upper()
        if upper not in SUPPORTED_CURRENCIES:
            raise ValueError(f"Only {', '.join(sorted(SUPPORTED_CURRENCIES))} is supported.")
        return upper

    @field_validator("limits")
    @classmethod
    def _limits(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        return validate_limits(value)


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


class LimitRead(BaseModel):
    key: LimitKey
    limit: int | None


class PlanVersionRead(BaseModel):
    id: uuid.UUID
    version: int
    name: str
    price: str
    currency: str
    interval: BillingInterval
    trial_days: int
    limits: list[LimitRead]
    effective_at: datetime
    created_at: datetime
    created_by: uuid.UUID | None
    reason: str | None
    subscribers: int = 0

    @classmethod
    def from_model(cls, version: PlanVersion, *, subscribers: int = 0) -> Self:
        return cls(
            id=version.id,
            version=version.version,
            name=version.name,
            price=f"{version.price:.2f}",
            currency=version.currency,
            interval=version.interval,
            trial_days=version.trial_days,
            limits=[LimitRead(key=key, limit=version.limit_for(key)) for key in LimitKey],
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
    ) -> Self:
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
                PlanVersionRead.from_model(current, subscribers=counts.get(current.id, 0))
                if current is not None
                else None
            ),
            latest_version=(
                PlanVersionRead.from_model(latest, subscribers=counts.get(latest.id, 0))
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
    key: LimitKey
    description: str
    unit: str
    kind: Literal["hard_limit", "meter_only", "account_limit"]
    enforcement: str
    concurrency_safe: bool
    unlimited: str = "null"


class LimitChange(BaseModel):
    key: LimitKey
    old: int | None
    new: int | None
    workspaces_above_new_limit: int | None


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
    model_config = ConfigDict(extra="forbid")

    plan_version_id: uuid.UUID
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
    id: uuid.UUID
    tenant_id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str | None
    plan_version_id: uuid.UUID | None
    plan_version: int | None
    status: SubscriptionStatus
    current_period_start: datetime
    current_period_end: datetime
    billing_anchor_at: datetime | None
    cancel_at_period_end: bool
    cancelled_at: datetime | None
    ended_at: datetime | None
    scheduled_plan_version_id: uuid.UUID | None
    scheduled_change_source: ScheduledChangeSource | None
    revision: int

    @classmethod
    def build(
        cls,
        subscription: Subscription,
        *,
        plan_code: str | None,
        version: int | None,
    ) -> Self:
        return cls(
            id=subscription.id,
            tenant_id=subscription.tenant_id,
            plan_id=subscription.plan_id,
            plan_code=plan_code,
            plan_version_id=subscription.plan_version_id,
            plan_version=version,
            status=subscription.status,
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            billing_anchor_at=subscription.billing_anchor_at,
            cancel_at_period_end=subscription.cancel_at_period_end,
            cancelled_at=subscription.cancelled_at,
            ended_at=subscription.ended_at,
            scheduled_plan_version_id=subscription.scheduled_plan_version_id,
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
