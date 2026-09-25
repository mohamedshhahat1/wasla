"""The platform billing control plane: `/platform/billing/*` (BILL-12, -13, -15).

Everything platform staff need to run billing without SQL - the plan catalogue
and its versions, subscriptions, invoices, payments, refunds, reconciliation and
incidents.

**Platform authority only.** Every route takes `PlatformStaffDep` (platform
owner or admin) except hard-deleting a plan, which takes `PlatformOwnerDep`.
Tenant roles, including a workspace owner, get 403 on every route here: a
customer able to reach these could price their own plan, mark their own invoice
paid or refund themselves.

**Every mutation** carries a reason and, where it changes an existing row, the
revision it was based on - a stale edit is 409, never a silent overwrite - and
writes an audit entry with the actor, their platform role, before and after, and
the request id.

**Every list** is paginated, `limit` at most 100.

Nothing here returns a card token, its envelope or fingerprint, a Paymob secret,
an API key, a payment key or a client secret.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import PlatformAccessAuditDep, PlatformOwnerDep, PlatformStaffDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.db.models.billing import PlanScope, SubscriptionStatus
from app.db.models.billing_incident import BillingIncidentKind, BillingIncidentStatus
from app.db.models.invoice import InvoicePurpose, InvoiceStatus, PaymentStatus
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.plan_admin import PlanCatalogAdmin
from app.schemas.platform_billing import (
    MAX_PAGE,
    FeatureRead,
    IncidentRead,
    IncidentResolve,
    InvoiceVoid,
    ManualPaymentCreate,
    MigrationRead,
    Page,
    PlanCreate,
    PlanMigrationCreate,
    PlanStateChange,
    PlanUpdate,
    PlanVersionCreate,
    PlanVersionPreview,
    PlanVersionPreviewRequest,
    PlanVersionRead,
    PlatformInvoiceRead,
    PlatformPaymentRead,
    PlatformPlanRead,
    PlatformRefundCreate,
    PlatformSubscriptionRead,
    ReconciliationRunResult,
    ReconciliationView,
    SubscriptionCancel,
    SubscriptionChangePlan,
    SubscriptionResume,
    TimelineEntry,
)
from app.schemas.text import StorableText

router = APIRouter(route_class=CommittingRoute, prefix="/platform/billing", tags=["platform"])

LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
OffsetQuery = Annotated[int, Query(ge=0, le=1_000_000)]
# Validated text (SEC-04): NUL and control characters are refused with a 422
# before any value reaches SQL.
CodeQuery = Annotated[StorableText | None, Query(min_length=1, max_length=50)]


def _read_scope(result: object) -> dict[str, Any]:
    """What an access entry records about a read: how many rows, whose.

    A page records its size; a single row records the workspace it belongs
    to, so the read is findable from that workspace's side (ADR-095).
    """
    if isinstance(result, Page):
        return {"returned": len(result.items)}
    if isinstance(result, list):
        return {"returned": len(result)}
    tenant_id = getattr(result, "tenant_id", None)
    return {"tenant_id": tenant_id} if isinstance(tenant_id, uuid.UUID) else {}


def get_plan_admin(session: SessionDep) -> PlanCatalogAdmin:
    return PlanCatalogAdmin(session)


def get_operations(session: SessionDep, settings: SettingsDep) -> PlatformBillingOperations:
    return PlatformBillingOperations(session, settings=settings)


PlanAdminDep = Annotated[PlanCatalogAdmin, Depends(get_plan_admin)]
OperationsDep = Annotated[PlatformBillingOperations, Depends(get_operations)]


# ------------------------------------------------------------------ plans


@router.get("/features", response_model=list[FeatureRead])
async def list_features(
    staff: PlatformStaffDep, access: PlatformAccessAuditDep, plans: PlanAdminDep
) -> list[FeatureRead]:
    """The entitlement keys Wasla enforces, how, and how `null` means unlimited."""
    result = plans.features()
    access.billing_read(actor=staff.user, resource="features", **_read_scope(result))
    return result


@router.get("/plans", response_model=Page[PlatformPlanRead])
async def list_plans(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    plans: PlanAdminDep,
    active: bool | None = None,
    public: bool | None = None,
    code: CodeQuery = None,
    currency: Annotated[
        str | None, Query(min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")
    ] = None,
    scope: PlanScope | None = None,
    tenant_id: uuid.UUID | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformPlanRead]:
    """The catalogue. `scope=tenant&tenant_id=...` lists one company's custom plans."""
    page = await plans.list_plans(
        active=active,
        public=public,
        code=code,
        currency=currency,
        scope=scope,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformPlanRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="plans", **_read_scope(result))
    return result


@router.post("/plans", response_model=PlatformPlanRead, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate, staff: PlatformStaffDep, plans: PlanAdminDep
) -> PlatformPlanRead:
    """A new plan and its version 1. The code is permanent."""
    return await plans.create(payload, actor=staff.user)


@router.get("/plans/{plan_id}", response_model=PlatformPlanRead)
async def get_plan(
    plan_id: uuid.UUID, staff: PlatformStaffDep, access: PlatformAccessAuditDep, plans: PlanAdminDep
) -> PlatformPlanRead:
    result = await plans.get(plan_id)
    access.billing_read(actor=staff.user, resource="plan", **_read_scope(result))
    return result


@router.patch("/plans/{plan_id}", response_model=PlatformPlanRead)
async def update_plan(
    plan_id: uuid.UUID, payload: PlanUpdate, staff: PlatformStaffDep, plans: PlanAdminDep
) -> PlatformPlanRead:
    """Name, description, visibility and order only. Price and limits change by
    publishing a version (`POST .../versions`), never here."""
    return await plans.update(plan_id, payload, actor=staff.user)


@router.post("/plans/{plan_id}/activate", response_model=PlatformPlanRead)
async def activate_plan(
    plan_id: uuid.UUID, payload: PlanStateChange, staff: PlatformStaffDep, plans: PlanAdminDep
) -> PlatformPlanRead:
    return await plans.set_active(
        plan_id,
        active=True,
        expected_revision=payload.expected_revision,
        reason=payload.reason,
        actor=staff.user,
    )


@router.post("/plans/{plan_id}/deactivate", response_model=PlatformPlanRead)
async def deactivate_plan(
    plan_id: uuid.UUID, payload: PlanStateChange, staff: PlatformStaffDep, plans: PlanAdminDep
) -> PlatformPlanRead:
    """No new checkouts. Existing subscribers keep their version and renew on it."""
    return await plans.set_active(
        plan_id,
        active=False,
        expected_revision=payload.expected_revision,
        reason=payload.reason,
        actor=staff.user,
    )


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(
    plan_id: uuid.UUID,
    owner: PlatformOwnerDep,
    plans: PlanAdminDep,
    reason: Annotated[StorableText, Query(min_length=3, max_length=500)],
) -> None:
    """Platform owner only, and only for a plan nothing ever referenced (409)."""
    await plans.delete(plan_id, reason=reason, actor=owner.user)


@router.get("/plans/{plan_id}/versions", response_model=list[PlanVersionRead])
async def list_versions(
    plan_id: uuid.UUID, staff: PlatformStaffDep, access: PlatformAccessAuditDep, plans: PlanAdminDep
) -> list[PlanVersionRead]:
    result = await plans.versions(plan_id)
    access.billing_read(actor=staff.user, resource="plan_versions", **_read_scope(result))
    return result


@router.post(
    "/plans/{plan_id}/versions",
    response_model=PlanVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    plan_id: uuid.UUID, payload: PlanVersionCreate, staff: PlatformStaffDep, plans: PlanAdminDep
) -> PlanVersionRead:
    """Publish new terms for new customers. `expected_version` must be current (409)."""
    return await plans.create_version(plan_id, payload, actor=staff.user)


@router.post("/plans/{plan_id}/versions/preview", response_model=PlanVersionPreview)
async def preview_version(
    plan_id: uuid.UUID,
    payload: PlanVersionPreviewRequest,
    staff: PlatformStaffDep,
    plans: PlanAdminDep,
) -> PlanVersionPreview:
    """What these terms would mean for whom. Writes nothing."""
    return await plans.preview(plan_id, payload)


@router.post("/plans/{plan_id}/migrations", response_model=MigrationRead)
async def schedule_migration(
    plan_id: uuid.UUID, payload: PlanMigrationCreate, staff: PlatformStaffDep, plans: PlanAdminDep
) -> MigrationRead:
    """Move a version's subscribers to another at each next renewal.

    `confirm: false` (the default) previews the count and writes nothing.
    """
    return await plans.schedule_migration(plan_id, payload, actor=staff.user)


# ---------------------------------------------------------- subscriptions


@router.get("/subscriptions", response_model=Page[PlatformSubscriptionRead])
async def list_subscriptions(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
    tenant_id: uuid.UUID | None = None,
    plan: CodeQuery = None,
    subscription_status: Annotated[SubscriptionStatus | None, Query(alias="status")] = None,
    cancel_at_period_end: bool | None = None,
    renews_before: datetime | None = None,
    renews_after: datetime | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformSubscriptionRead]:
    page = await operations.list_subscriptions(
        tenant_id=tenant_id,
        plan_code=plan,
        status=subscription_status,
        cancel_at_period_end=cancel_at_period_end,
        renews_before=renews_before,
        renews_after=renews_after,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformSubscriptionRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="subscriptions", **_read_scope(result))
    return result


@router.get("/subscriptions/{subscription_id}", response_model=PlatformSubscriptionRead)
async def get_subscription(
    subscription_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
) -> PlatformSubscriptionRead:
    result = await operations.get_subscription(subscription_id)
    access.billing_read(actor=staff.user, resource="subscription", **_read_scope(result))
    return result


@router.get("/subscriptions/{subscription_id}/timeline", response_model=list[TimelineEntry])
async def subscription_timeline(
    subscription_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
) -> list[TimelineEntry]:
    result = await operations.timeline(subscription_id)
    access.billing_read(actor=staff.user, resource="subscription_timeline", **_read_scope(result))
    return result


@router.post(
    "/subscriptions/{subscription_id}/change-plan", response_model=PlatformSubscriptionRead
)
async def change_subscription_plan(
    subscription_id: uuid.UUID,
    payload: SubscriptionChangePlan,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformSubscriptionRead:
    """`next_renewal` schedules; `now` to a priced version needs a financial basis."""
    return await operations.change_plan(subscription_id, payload, actor=staff.user)


@router.post("/subscriptions/{subscription_id}/cancel", response_model=PlatformSubscriptionRead)
async def cancel_subscription(
    subscription_id: uuid.UUID,
    payload: SubscriptionCancel,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformSubscriptionRead:
    return await operations.cancel_subscription(subscription_id, payload, actor=staff.user)


@router.post("/subscriptions/{subscription_id}/resume", response_model=PlatformSubscriptionRead)
async def resume_subscription(
    subscription_id: uuid.UUID,
    payload: SubscriptionResume,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformSubscriptionRead:
    return await operations.resume_subscription(subscription_id, payload, actor=staff.user)


# --------------------------------------------------------------- invoices


@router.get("/invoices", response_model=Page[PlatformInvoiceRead])
async def list_invoices(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
    tenant_id: uuid.UUID | None = None,
    subscription_id: uuid.UUID | None = None,
    invoice_status: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    purpose: InvoicePurpose | None = None,
    plan: CodeQuery = None,
    issued_from: datetime | None = None,
    issued_until: datetime | None = None,
    overdue_before: datetime | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformInvoiceRead]:
    page = await operations.list_invoices(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        status=invoice_status,
        purpose=purpose,
        plan_code=plan,
        issued_from=issued_from,
        issued_until=issued_until,
        overdue_before=overdue_before,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformInvoiceRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="invoices", **_read_scope(result))
    return result


@router.get("/invoices/{invoice_id}", response_model=PlatformInvoiceRead)
async def get_invoice(
    invoice_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
) -> PlatformInvoiceRead:
    result = await operations.get_invoice(invoice_id)
    access.billing_read(actor=staff.user, resource="invoice", **_read_scope(result))
    return result


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=PlatformPaymentRead,
    status_code=status.HTTP_201_CREATED,
)
async def record_manual_payment(
    invoice_id: uuid.UUID,
    payload: ManualPaymentCreate,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformPaymentRead:
    """Money seen arriving outside the product, settled like any other (BILL-10)."""
    return await operations.record_manual_payment(invoice_id, payload, actor=staff.user)


@router.post("/invoices/{invoice_id}/void", response_model=PlatformInvoiceRead)
async def void_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceVoid,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformInvoiceRead:
    return await operations.void_invoice(invoice_id, payload, actor=staff.user)


# --------------------------------------------------------------- payments


@router.get("/payments", response_model=Page[PlatformPaymentRead])
async def list_payments(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
    tenant_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    payment_status: Annotated[PaymentStatus | None, Query(alias="status")] = None,
    provider: CodeQuery = None,
    automatic: bool | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformPaymentRead]:
    page = await operations.list_payments(
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        status=payment_status,
        provider=provider,
        automatic=automatic,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformPaymentRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="payments", **_read_scope(result))
    return result


@router.get("/payments/{payment_id}", response_model=PlatformPaymentRead)
async def get_payment(
    payment_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
) -> PlatformPaymentRead:
    result = await operations.get_payment(payment_id)
    access.billing_read(actor=staff.user, resource="payment", **_read_scope(result))
    return result


@router.post(
    "/payments/{payment_id}/refund",
    response_model=PlatformPaymentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refund_payment(
    payment_id: uuid.UUID,
    payload: PlatformRefundCreate,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> PlatformPaymentRead:
    """Ask the provider for a refund. 202: confirmed later by its signed callback."""
    return await operations.refund(payment_id, payload, actor=staff.user)


# --------------------------------------------------------- reconciliation


@router.get("/reconciliation", response_model=ReconciliationView)
async def reconciliation(
    staff: PlatformStaffDep, access: PlatformAccessAuditDep, operations: OperationsDep
) -> ReconciliationView:
    result = await operations.reconciliation()
    access.billing_read(actor=staff.user, resource="reconciliation", **_read_scope(result))
    return result


@router.post("/reconciliation/{payment_id}/run", response_model=ReconciliationRunResult)
async def run_reconciliation(
    payment_id: uuid.UUID, staff: PlatformStaffDep, operations: OperationsDep
) -> ReconciliationRunResult:
    """Ask the provider about one payment and apply the answer. Never charges."""
    return await operations.run_reconciliation(payment_id, actor=staff.user)


@router.get("/incidents", response_model=Page[IncidentRead])
async def list_incidents(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    operations: OperationsDep,
    incident_status: Annotated[BillingIncidentStatus | None, Query(alias="status")] = None,
    kind: BillingIncidentKind | None = None,
    tenant_id: uuid.UUID | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[IncidentRead]:
    page = await operations.list_incidents(
        status=incident_status, kind=kind, tenant_id=tenant_id, limit=limit, offset=offset
    )
    result: Page[IncidentRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="incidents", **_read_scope(result))
    return result


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentRead)
async def resolve_incident(
    incident_id: uuid.UUID,
    payload: IncidentResolve,
    staff: PlatformStaffDep,
    operations: OperationsDep,
) -> IncidentRead:
    return await operations.resolve_incident(incident_id, note=payload.note, actor=staff.user)
