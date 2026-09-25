"""Assembling one company's billing summary for platform staff (ADR-113).

Reads only. Every figure comes from the service that owns it - the entitlement
breakdown from `EntitlementService`, the pinned terms from `PlanCatalog` - so
the summary cannot disagree with what the workspace is actually held to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.db.models.audit import AuditLog
from app.db.models.billing import TOPUP_LIMITS, LimitKey, Plan, PlanVersion
from app.db.models.billing_incident import BillingIncident, BillingIncidentStatus
from app.db.models.invoice import Invoice, Payment
from app.db.models.tenant import Tenant
from app.platform.topup_admin import TopupAdmin
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.topup_repository import TopupPurchaseRepository
from app.schemas.billing import EntitlementRead
from app.schemas.billing_summary import (
    SUMMARY_RECENT,
    SUMMARY_TIMELINE,
    PlatformTenantBillingSummary,
    SummaryPlan,
    SummaryScheduledChange,
    SummaryTenant,
)
from app.schemas.platform_billing import (
    IncidentRead,
    PlanVersionRead,
    PlatformInvoiceRead,
    PlatformPaymentRead,
    PlatformSubscriptionRead,
    TimelineEntry,
)
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog

#: The entitlements a billing page shows, in the order it shows them.
SUMMARY_KEYS: tuple[LimitKey, ...] = tuple(key for key in LimitKey if key in TOPUP_LIMITS)

# Audit targets that belong on a company's billing timeline.
_TIMELINE_TARGETS = ("subscription", "invoice", "payment", "topup_purchase", "plan")


class TenantBillingSummaryBuilder:
    """The platform's one-request view of a company's billing."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def build(
        self, tenant_id: uuid.UUID, *, now: datetime | None = None
    ) -> PlatformTenantBillingSummary:
        moment = now if now is not None else datetime.now(UTC)
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("No such workspace.")
        subscription = await SubscriptionRepository(self._session, tenant_id=tenant.id).get()
        catalog = PlanCatalog(self._session)

        plan: Plan | None = None
        version: PlanVersion | None = None
        subscription_read: PlatformSubscriptionRead | None = None
        scheduled: SummaryScheduledChange | None = None
        if subscription is not None:
            plan = await self._session.get(Plan, subscription.plan_id)
            version = await catalog.pinned_version(subscription)
            subscription_read = PlatformSubscriptionRead.build(
                subscription,
                plan_code=plan.code if plan is not None else None,
                version=version.version if version is not None else None,
            )
            if subscription.scheduled_plan_version_id is not None:
                target = await catalog.get_version(subscription.scheduled_plan_version_id)
                target_plan = (
                    await self._session.get(Plan, target.plan_id) if target is not None else None
                )
                scheduled = SummaryScheduledChange(
                    plan_version_id=subscription.scheduled_plan_version_id,
                    plan_code=target_plan.code if target_plan is not None else None,
                    version=target.version if target is not None else None,
                    price=f"{target.price:.2f}" if target is not None else None,
                    source=subscription.scheduled_change_source,
                    effective_at=subscription.current_period_end,
                )

        entitlements = EntitlementService(
            self._session,
            tenant_id=tenant.id,
            default_plan_code=self._settings.default_plan_code,
            clock=lambda: moment,
        )
        breakdown = [
            EntitlementRead.from_entitlement(item)
            for item in await entitlements.snapshot(SUMMARY_KEYS)
        ]
        admin = TopupAdmin(self._session, settings=self._settings)
        active = await TopupPurchaseRepository(self._session, tenant_id=tenant.id).active(at=moment)

        renews = (
            subscription is not None
            and subscription.is_serving
            and not subscription.cancel_at_period_end
        )
        return PlatformTenantBillingSummary(
            tenant=SummaryTenant(
                id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status
            ),
            subscription=subscription_read,
            plan=(
                SummaryPlan(
                    id=plan.id,
                    code=plan.code,
                    name=plan.name,
                    scope=plan.scope,
                    is_custom=plan.is_custom,
                    is_active=plan.is_active,
                )
                if plan is not None
                else None
            ),
            plan_version=PlanVersionRead.from_model(version) if version is not None else None,
            custom_plan=plan is not None and plan.is_custom,
            price=f"{version.price:.2f}" if version is not None else None,
            currency=version.currency if version is not None else None,
            current_period_start=subscription.current_period_start if subscription else None,
            current_period_end=subscription.current_period_end if subscription else None,
            next_renewal_at=(
                subscription.current_period_end if renews and subscription is not None else None
            ),
            scheduled_change=scheduled,
            entitlements=breakdown,
            active_topups=[await admin.read_purchase(row) for row in active],
            recent_invoices=[
                PlatformInvoiceRead.from_model(row)
                for row in (
                    await self._session.scalars(
                        select(Invoice)
                        .where(Invoice.tenant_id == tenant.id)
                        .order_by(Invoice.created_at.desc(), Invoice.id)
                        .limit(SUMMARY_RECENT)
                    )
                ).all()
            ],
            recent_payments=[
                PlatformPaymentRead.from_model(row)
                for row in (
                    await self._session.scalars(
                        select(Payment)
                        .where(Payment.tenant_id == tenant.id)
                        .order_by(Payment.created_at.desc(), Payment.id)
                        .limit(SUMMARY_RECENT)
                    )
                ).all()
            ],
            open_incidents=[
                IncidentRead.from_model(row)
                for row in (
                    await self._session.scalars(
                        select(BillingIncident)
                        .where(BillingIncident.tenant_id == tenant.id)
                        .where(BillingIncident.status == BillingIncidentStatus.OPEN)
                        .order_by(BillingIncident.created_at.desc())
                        .limit(SUMMARY_RECENT)
                    )
                ).all()
            ],
            timeline=[
                TimelineEntry(
                    occurred_at=row.occurred_at,
                    kind="audit",
                    action=row.action.value,
                    actor=row.actor_label,
                    target_id=row.target_id,
                    detail=row.meta,
                )
                for row in (
                    await self._session.scalars(
                        select(AuditLog)
                        .where(AuditLog.tenant_id == tenant.id)
                        .where(AuditLog.target_type.in_(_TIMELINE_TARGETS))
                        .order_by(AuditLog.occurred_at.desc())
                        .limit(SUMMARY_TIMELINE)
                    )
                ).all()
            ],
        )


__all__ = ["SUMMARY_KEYS", "TenantBillingSummaryBuilder"]
