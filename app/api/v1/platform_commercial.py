"""Custom plans and top-ups on the platform billing control plane (ADR-113).

Same prefix, same authority and same contract as `platform_billing`: platform
owner or admin on every route - owner only for hard-deleting a product - and 403
for every tenant role. Every mutation carries a reason and, where it changes an
existing row, the revision it was based on (409 when stale), and is audited.
Every read records a `platform_billing_read` access entry. Lists are paged,
`limit` at most 100. Nothing here returns a token, a secret or a payment key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import PlatformAccessAuditDep, PlatformOwnerDep, PlatformStaffDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.db.models.topup import TopupEntitlement, TopupScope, TopupSource, TopupStatus
from app.platform.custom_plan_admin import CustomPlanAdmin
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.tenant_billing_summary import TenantBillingSummaryBuilder
from app.platform.topup_admin import TopupAdmin
from app.schemas.billing_summary import PlatformTenantBillingSummary
from app.schemas.custom_plan import (
    CustomPlanCreate,
    CustomPlanOfferCancel,
    CustomPlanOfferCreate,
    CustomPlanOfferRead,
    CustomPlanPreview,
    CustomPlanPreviewRequest,
    CustomPlanResult,
)
from app.schemas.platform_billing import MAX_PAGE, Page
from app.schemas.text import StorableText
from app.schemas.topup import (
    PlatformTopupProductRead,
    PlatformTopupPurchaseRead,
    TopupGrantCreate,
    TopupProductCreate,
    TopupProductUpdate,
    TopupRefundReview,
    TopupStateChange,
)
from app.services.custom_plan_offer_service import offer_read

router = APIRouter(route_class=CommittingRoute, prefix="/platform/billing", tags=["platform"])

LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
OffsetQuery = Annotated[int, Query(ge=0, le=1_000_000)]


def get_custom_plans(session: SessionDep, settings: SettingsDep) -> CustomPlanAdmin:
    return CustomPlanAdmin(session, settings=settings)


def get_topups(session: SessionDep, settings: SettingsDep) -> TopupAdmin:
    return TopupAdmin(session, settings=settings)


def get_summaries(session: SessionDep, settings: SettingsDep) -> TenantBillingSummaryBuilder:
    return TenantBillingSummaryBuilder(session, settings=settings)


def get_offers(session: SessionDep) -> PlatformCustomPlanOffers:
    return PlatformCustomPlanOffers(session)


CustomPlansDep = Annotated[CustomPlanAdmin, Depends(get_custom_plans)]
OffersDep = Annotated[PlatformCustomPlanOffers, Depends(get_offers)]
TopupsDep = Annotated[TopupAdmin, Depends(get_topups)]
SummariesDep = Annotated[TenantBillingSummaryBuilder, Depends(get_summaries)]


def _scope(result: object) -> dict[str, Any]:
    """What an access entry records about a read (ADR-095)."""
    if isinstance(result, Page):
        return {"returned": len(result.items)}
    tenant_id = getattr(result, "tenant_id", None)
    if isinstance(tenant_id, uuid.UUID):
        return {"tenant_id": tenant_id}
    tenant = getattr(result, "tenant", None)
    tenant_ref = getattr(tenant, "id", None)
    return {"tenant_id": tenant_ref} if isinstance(tenant_ref, uuid.UUID) else {}


# ------------------------------------------------------------ custom plans


@router.post(
    "/tenants/{tenant_id}/custom-plan/preview",
    response_model=CustomPlanPreview,
)
async def preview_custom_plan(
    tenant_id: uuid.UUID,
    payload: CustomPlanPreviewRequest,
    staff: PlatformStaffDep,
    plans: CustomPlansDep,
) -> CustomPlanPreview:
    """What a custom plan with these terms would mean for this company. Writes nothing."""
    return await plans.preview(tenant_id, payload)


@router.post(
    "/tenants/{tenant_id}/custom-plan",
    response_model=CustomPlanResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_plan(
    tenant_id: uuid.UUID,
    payload: CustomPlanCreate,
    staff: PlatformStaffDep,
    plans: CustomPlansDep,
) -> CustomPlanResult:
    """A TENANT-scoped plan and its version 1, optionally assigned now or at renewal.

    A priced plan applied now needs a `financial_basis`; `customer_checkout`
    assigns nothing and makes an **offer** the company's owner accepts and
    pays (ADR-114). The plan applies only when that payment is confirmed.
    """
    return await plans.create(tenant_id, payload, actor=staff.user)


@router.get(
    "/tenants/{tenant_id}/custom-offers",
    response_model=list[CustomPlanOfferRead],
)
async def list_custom_offers(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    offers: OffersDep,
    session: SessionDep,
) -> list[CustomPlanOfferRead]:
    """Every custom plan offer made to this company, newest first."""
    rows = await offers.list_for_tenant(tenant_id)
    now = datetime.now(UTC)
    access.billing_read(actor=staff.user, resource="custom_plan_offers", tenant_id=tenant_id)
    return [await offer_read(session, row, now=now) for row in rows]


@router.post(
    "/tenants/{tenant_id}/custom-offers",
    response_model=CustomPlanOfferRead,
    status_code=status.HTTP_201_CREATED,
)
async def offer_custom_plan(
    tenant_id: uuid.UUID,
    payload: CustomPlanOfferCreate,
    staff: PlatformStaffDep,
    offers: OffersDep,
    session: SessionDep,
) -> CustomPlanOfferRead:
    """Offer one version of this company's own priced custom plan. Grants nothing."""
    offer = await offers.offer(tenant_id, payload, actor=staff.user)
    return await offer_read(session, offer, now=datetime.now(UTC))


@router.post(
    "/custom-offers/{offer_id}/cancel",
    response_model=CustomPlanOfferRead,
)
async def cancel_custom_offer(
    offer_id: uuid.UUID,
    payload: CustomPlanOfferCancel,
    staff: PlatformStaffDep,
    offers: OffersDep,
    session: SessionDep,
) -> CustomPlanOfferRead:
    """Withdraw an open offer. Money for it arriving afterwards is held, not granted."""
    offer = await offers.cancel(offer_id, payload, actor=staff.user)
    return await offer_read(session, offer, now=datetime.now(UTC))


@router.get(
    "/tenants/{tenant_id}/summary",
    response_model=PlatformTenantBillingSummary,
)
async def tenant_billing_summary(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    summaries: SummariesDep,
) -> PlatformTenantBillingSummary:
    """Plan, period, entitlements, top-ups, recent money and incidents, in one read."""
    result = await summaries.build(tenant_id)
    access.billing_read(actor=staff.user, resource="tenant_billing_summary", **_scope(result))
    return result


@router.post(
    "/tenants/{tenant_id}/topups/grant",
    response_model=PlatformTopupPurchaseRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_topup(
    tenant_id: uuid.UUID,
    payload: TopupGrantCreate,
    staff: PlatformStaffDep,
    topups: TopupsDep,
) -> PlatformTopupPurchaseRead:
    """Complimentary allowance until the current period ends. Never a payment."""
    return await topups.grant(tenant_id, payload, actor=staff.user)


# ------------------------------------------------------------------ products


@router.get("/topups", response_model=Page[PlatformTopupProductRead])
async def list_topup_products(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    topups: TopupsDep,
    scope: TopupScope | None = None,
    tenant_id: uuid.UUID | None = None,
    entitlement_key: TopupEntitlement | None = None,
    active: bool | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformTopupProductRead]:
    page = await topups.list_products(
        scope=scope,
        tenant_id=tenant_id,
        entitlement=entitlement_key,
        active=active,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformTopupProductRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="topup_products", **_scope(result))
    return result


@router.post(
    "/topups", response_model=PlatformTopupProductRead, status_code=status.HTTP_201_CREATED
)
async def create_topup_product(
    payload: TopupProductCreate, staff: PlatformStaffDep, topups: TopupsDep
) -> PlatformTopupProductRead:
    """A new product. Its code, entitlement, scope and owner are permanent."""
    return await topups.create_product(payload, actor=staff.user)


@router.get("/topups/{topup_id}", response_model=PlatformTopupProductRead)
async def get_topup_product(
    topup_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    topups: TopupsDep,
) -> PlatformTopupProductRead:
    result = await topups.get_product(topup_id)
    access.billing_read(actor=staff.user, resource="topup_product", **_scope(result))
    return result


@router.patch("/topups/{topup_id}", response_model=PlatformTopupProductRead)
async def update_topup_product(
    topup_id: uuid.UUID,
    payload: TopupProductUpdate,
    staff: PlatformStaffDep,
    topups: TopupsDep,
) -> PlatformTopupProductRead:
    """New terms for future purchases. Existing purchases keep what they bought."""
    return await topups.update_product(topup_id, payload, actor=staff.user)


@router.post("/topups/{topup_id}/activate", response_model=PlatformTopupProductRead)
async def activate_topup_product(
    topup_id: uuid.UUID, payload: TopupStateChange, staff: PlatformStaffDep, topups: TopupsDep
) -> PlatformTopupProductRead:
    return await topups.set_active(
        topup_id,
        active=True,
        expected_revision=payload.expected_revision,
        reason=payload.reason,
        actor=staff.user,
    )


@router.post("/topups/{topup_id}/deactivate", response_model=PlatformTopupProductRead)
async def deactivate_topup_product(
    topup_id: uuid.UUID, payload: TopupStateChange, staff: PlatformStaffDep, topups: TopupsDep
) -> PlatformTopupProductRead:
    """No new purchases. Paid purchases, live grants and history are untouched."""
    return await topups.set_active(
        topup_id,
        active=False,
        expected_revision=payload.expected_revision,
        reason=payload.reason,
        actor=staff.user,
    )


@router.delete("/topups/{topup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topup_product(
    topup_id: uuid.UUID,
    owner: PlatformOwnerDep,
    topups: TopupsDep,
    reason: Annotated[StorableText, Query(min_length=3, max_length=500)],
) -> None:
    """Platform owner only, and only for a product nobody ever bought (409)."""
    await topups.delete_product(topup_id, reason=reason, actor=owner.user)


# ----------------------------------------------------------------- purchases


@router.get("/topup-purchases", response_model=Page[PlatformTopupPurchaseRead])
async def list_topup_purchases(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    topups: TopupsDep,
    tenant_id: uuid.UUID | None = None,
    purchase_status: Annotated[TopupStatus | None, Query(alias="status")] = None,
    source: TopupSource | None = None,
    entitlement_key: TopupEntitlement | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformTopupPurchaseRead]:
    page = await topups.list_purchases(
        tenant_id=tenant_id,
        status=purchase_status,
        source=source,
        entitlement=entitlement_key,
        limit=limit,
        offset=offset,
    )
    result: Page[PlatformTopupPurchaseRead] = Page(
        items=page.items, total=page.total, limit=limit, offset=offset
    )
    access.billing_read(actor=staff.user, resource="topup_purchases", **_scope(result))
    return result


@router.get("/topup-purchases/{purchase_id}", response_model=PlatformTopupPurchaseRead)
async def get_topup_purchase(
    purchase_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    topups: TopupsDep,
) -> PlatformTopupPurchaseRead:
    result = await topups.get_purchase(purchase_id)
    access.billing_read(actor=staff.user, resource="topup_purchase", **_scope(result))
    return result


@router.post(
    "/topup-purchases/{purchase_id}/refund-review", response_model=PlatformTopupPurchaseRead
)
async def review_topup_refund(
    purchase_id: uuid.UUID,
    payload: TopupRefundReview,
    staff: PlatformStaffDep,
    topups: TopupsDep,
) -> PlatformTopupPurchaseRead:
    """Keep or withdraw a refunded top-up's allowance. Withdrawal deletes nothing."""
    return await topups.review_refund(purchase_id, payload, actor=staff.user)
