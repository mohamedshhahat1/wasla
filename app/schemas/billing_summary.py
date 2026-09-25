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

from app.db.models.billing import PlanScope, ScheduledChangeSource
from app.db.models.enums import TenantStatus
from app.schemas.billing import EntitlementRead
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
    plan_version_id: uuid.UUID
    plan_code: str | None
    version: int | None
    price: str | None
    source: ScheduledChangeSource | None
    effective_at: datetime


class PlatformTenantBillingSummary(BaseModel):
    tenant: SummaryTenant
    subscription: PlatformSubscriptionRead | None
    plan: SummaryPlan | None
    plan_version: PlanVersionRead | None
    custom_plan: bool
    price: str | None
    currency: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    # Null when nothing will renew: a cancellation takes effect at period end,
    # or the subscription has stopped.
    next_renewal_at: datetime | None
    scheduled_change: SummaryScheduledChange | None
    entitlements: list[EntitlementRead]
    active_topups: list[PlatformTopupPurchaseRead]
    recent_invoices: list[PlatformInvoiceRead]
    recent_payments: list[PlatformPaymentRead]
    open_incidents: list[IncidentRead]
    timeline: list[TimelineEntry]
