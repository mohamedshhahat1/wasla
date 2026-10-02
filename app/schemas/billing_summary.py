"""One company's billing, in one response, for the platform's company page (ADR-113).

Everything the "Company -> Billing" screen shows without a dozen requests: the
plan and version the workspace is held to (and whether it is a custom plan), the
period, the next renewal, a scheduled change, the seven entitlements broken into
base, top-ups and grants against usage, the live top-ups, and bounded recent
invoices, payments, incidents and timeline. Collections are capped - this is a
summary, and each has its own paged endpoint for the rest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models.billing import BillingInterval, PlanScope, ScheduledChangeSource
from app.db.models.enums import TenantStatus
from app.schemas.billing import EntitlementRead
from app.schemas.channel_capacity import CapacityReductionRead
from app.schemas.platform_billing import (
    IncidentRead,
    PlanVersionRead,
    PlatformInvoiceRead,
    PlatformPaymentRead,
    PlatformSubscriptionRead,
    TimelineEntry,
)
from app.schemas.topup import PlatformTopupPurchaseRead

#: How many of each recent collection the summary carries.
SUMMARY_RECENT = 10
SUMMARY_TIMELINE = 20


class SummaryTenant(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus


class SummaryPlan(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    scope: PlanScope
    is_custom: bool
    is_active: bool


class SummaryScheduledChange(BaseModel):
    """A change waiting for the end of the billing term, pinned to its price."""

    plan_version_id: uuid.UUID
    plan_price_id: uuid.UUID | None = None
    plan_code: str | None
    version: int | None
    price: str | None
    billing_interval: BillingInterval | None = None
    source: ScheduledChangeSource | None
    effective_at: datetime


class PlatformTenantBillingSummary(BaseModel):
    tenant: SummaryTenant
    subscription: PlatformSubscriptionRead | None
    plan: SummaryPlan | None
    plan_version: PlanVersionRead | None
    custom_plan: bool
    # The price the subscription renews at, and so its billing term (ADR-116).
    plan_price_id: uuid.UUID | None = None
    billing_interval: BillingInterval | None = None
    price: str | None
    currency: str | None
    # The paid billing term - a year on a yearly price.
    current_period_start: datetime | None
    current_period_end: datetime | None
    # The monthly usage cycle inside it, in force now.
    usage_period_start: datetime | None = None
    usage_period_end: datetime | None = None
    # Null when nothing will renew: a cancellation takes effect at period end,
    # or the subscription has stopped.
    next_renewal_at: datetime | None
    scheduled_change: SummaryScheduledChange | None
    # Includes `channel_connections` with its general and typed slots and
    # active connections by channel, and `period_ai_turns` with its open holds
    # and its charges by channel (ADR-131).
    entitlements: list[EntitlementRead]
    # The workspace's latest capacity reduction - open, with its grace end, or
    # how the last one ended (ENT-14, ENT-15). Null if it never had one.
    channel_capacity_reduction: CapacityReductionRead | None = None
    active_topups: list[PlatformTopupPurchaseRead]
    recent_invoices: list[PlatformInvoiceRead]
    recent_payments: list[PlatformPaymentRead]
    open_incidents: list[IncidentRead]
    timeline: list[TimelineEntry]
