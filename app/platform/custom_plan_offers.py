"""Offering a custom plan to its workspace, withdrawing it, and expiring it (ADR-114).

Platform staff make offers; the workspace's owner accepts or declines them
(`CustomPlanOfferService`); settlement activates one. Nothing here moves money
or changes a subscription: an offer is a statement of terms the customer may
buy, and the only thing that makes it the workspace's plan is an authenticated
payment for it.

Every mutation is audited with actor, platform role, reason, before and after.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.telemetry import record_custom_plan
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import Plan, PlanScope, PlanVersion
from app.db.models.custom_plan_offer import (
    CustomPlanOffer,
    CustomPlanOfferStatus,
    offer_may_move,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.platform.billing_audit import record_platform_billing
from app.repositories.custom_plan_offer_repository import PlatformCustomPlanOfferRepository
from app.schemas.custom_plan import CustomPlanOfferCancel, CustomPlanOfferCreate
from app.services.audit_service import AuditTrail

logger = get_logger(__name__)

# How many expired offers one sweep closes. Small: an offer expiring a sweep
# late changes nothing a customer can do (acceptance checks the clock).
EXPIRY_BATCH: Final = 100


def _state(offer: CustomPlanOffer) -> dict[str, object]:
    return {
        "status": offer.status.value,
        "plan_version_id": str(offer.plan_version_id),
        "expires_at": offer.expires_at.isoformat() if offer.expires_at else None,
    }


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class PlatformCustomPlanOffers:
    """Offers across workspaces, for platform staff and the billing sweep."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._offers = PlatformCustomPlanOfferRepository(session)

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[CustomPlanOffer]:
        await self._tenant(tenant_id)
        return await self._offers.for_tenant(tenant_id)

    async def require(self, offer_id: uuid.UUID) -> CustomPlanOffer:
        offer = await self._offers.get_by_id(offer_id)
        if offer is None:
            raise NotFoundError("No such offer.")
        return offer

    async def offer(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanOfferCreate,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> CustomPlanOffer:
        """Offer one version of the workspace's own priced custom plan to it.

        Refused (422) for a version of any plan that is not this workspace's
        custom plan, for a free version - a free custom plan is assigned, not
        sold - and for an expiry in the past. Refused (409) while another offer
        is open: the customer should never have to choose between two sets of
        terms from the same supplier without knowing which is current.
        """
        moment = now if now is not None else datetime.now(UTC)
        try:
            offer = await self._offer(tenant_id, payload, actor=actor, now=moment)
        except (ValidationError, ConflictError, NotFoundError):
            await record_custom_plan("offer", "refused")
            raise
        await record_custom_plan("offer", "succeeded")
        return offer

    async def _offer(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanOfferCreate,
        *,
        actor: User,
        now: datetime,
    ) -> CustomPlanOffer:
        tenant = await self._tenant(tenant_id)
        version = await self._session.get(PlanVersion, payload.plan_version_id)
        plan = await self._session.get(Plan, version.plan_id) if version is not None else None
        if version is None or plan is None:
            raise NotFoundError("No such plan version.")
        if plan.scope is not PlanScope.TENANT or plan.tenant_id != tenant.id:
            raise ValidationError(
                "Only the workspace's own custom plan can be offered to it.",
            )
        if not plan.is_active:
            raise ValidationError("A retired custom plan cannot be offered.")
        if version.price <= 0:
            raise ValidationError(
                "A free custom plan is not sold. Assign it with a complimentary basis instead."
            )
        expires_at = _aware(payload.expires_at) if payload.expires_at is not None else None
        if expires_at is not None and expires_at <= now:
            raise ValidationError("expires_at must be in the future.")

        offer = CustomPlanOffer(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            status=CustomPlanOfferStatus.OFFERED,
            expires_at=expires_at,
            reason=payload.reason,
            created_by=actor.id,
        )
        offer.created_at = now
        try:
            async with self._session.begin_nested():
                self._session.add(offer)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError(
                "This workspace already has an open offer. Withdraw it before making another."
            ) from None

        record_platform_billing(
            self._session,
            AuditAction.BILLING_CUSTOM_PLAN_OFFERED,
            actor=actor,
            reason=payload.reason,
            target_type="custom_plan_offer",
            target_id=offer.id,
            tenant_id=tenant.id,
            target_label=plan.code,
            after={
                **_state(offer),
                "version": version.version,
                "price": str(version.price),
                "currency": version.currency,
                "interval": version.interval.value,
                "limits": dict(version.limits),
            },
        )
        logger.info(
            "billing.custom_plan_offered",
            extra={
                "event": "billing.custom_plan_offered",
                "tenant_id": str(tenant.id),
                "offer_id": str(offer.id),
                "plan_version_id": str(version.id),
            },
        )
        return offer

    async def cancel(
        self,
        offer_id: uuid.UUID,
        payload: CustomPlanOfferCancel,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> CustomPlanOffer:
        """Withdraw an open offer. Money for it afterwards is held, never granted."""
        moment = now if now is not None else datetime.now(UTC)
        offer = await self._lock(offer_id)
        if offer.revision != payload.expected_revision:
            await record_custom_plan("offer_cancel", "refused")
            raise ConflictError("The offer changed since it was read. Reload and try again.")
        if not offer_may_move(offer.status, CustomPlanOfferStatus.CANCELLED):
            await record_custom_plan("offer_cancel", "refused")
            raise ConflictError(
                "This offer can no longer be withdrawn.", details={"status": offer.status.value}
            )
        before = _state(offer)
        offer.status = CustomPlanOfferStatus.CANCELLED
        offer.cancelled_at = moment
        offer.cancelled_by = actor.id
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_CUSTOM_PLAN_OFFER_CANCELLED,
            actor=actor,
            reason=payload.reason,
            target_type="custom_plan_offer",
            target_id=offer.id,
            tenant_id=offer.tenant_id,
            before=before,
            after=_state(offer),
        )
        await record_custom_plan("offer_cancel", "succeeded")
        return offer

    async def expire_due(self, *, now: datetime) -> int:
        """Close open offers whose acceptance window has passed. The billing sweep's.

        A page opened before expiry may still be paid and is honoured at
        settlement; this only records that no *new* page may be opened.
        """
        expired = 0
        for offer in await self._offers.due_to_expire(at=now, limit=EXPIRY_BATCH):
            if not offer_may_move(offer.status, CustomPlanOfferStatus.EXPIRED):
                continue
            previous = offer.status
            offer.status = CustomPlanOfferStatus.EXPIRED
            offer.expired_at = now
            AuditTrail(self._session, tenant_id=offer.tenant_id).record(
                AuditAction.BILLING_CUSTOM_PLAN_OFFER_EXPIRED,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="custom_plan_offer",
                target_id=offer.id,
                meta={"from_status": previous.value},
            )
            expired += 1
        if expired:
            await self._session.flush()
            await record_custom_plan("offer_expire", "succeeded")
        return expired

    async def _lock(self, offer_id: uuid.UUID) -> CustomPlanOffer:
        offer = await self._offers.get_by_id(offer_id)
        if offer is None:
            raise NotFoundError("No such offer.")
        await self._session.refresh(offer, with_for_update=True)
        return offer

    async def _tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None or tenant.deleted_at is not None:
            raise NotFoundError("No such workspace.")
        return tenant


__all__ = ["PlatformCustomPlanOffers"]
