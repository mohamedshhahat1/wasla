"""What settling a custom plan offer's invoice means for the offer (ADR-114).

Kept apart from `CustomPlanOfferService` because settlement calls it, and
settlement sits beneath the checkout service the offer service is built on.

Two questions, both asked with the offer locked so they are answered once:

- **May this money buy the offer?** Not if the customer declined it or the
  platform withdrew it after the page was opened: the money is held and an
  incident raised, exactly like a page paid after a cancellation. An offer that
  expired is still honoured for a page opened *before* it expired, because the
  checkout snapshot is what the customer agreed to.
- **What does paying it do?** The subscription change is settlement's own
  (`apply_purchase` onto the invoice's pinned version); this only records that
  the offer is now active, once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.custom_plan_offer import CustomPlanOfferStatus, offer_may_move
from app.db.models.invoice import Invoice
from app.repositories.custom_plan_offer_repository import CustomPlanOfferRepository
from app.services.audit_service import AuditTrail

logger = get_logger(__name__)


class CustomPlanOfferLedger:
    """The offer side of settling one workspace's offer invoices."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._offers = CustomPlanOfferRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def refusal(self, invoice: Invoice) -> str | None:
        """Why this invoice's money may not buy its offer, or None."""
        if invoice.custom_plan_offer_id is None:
            return None
        offer = await self._offers.lock(invoice.custom_plan_offer_id)
        if offer is None:  # pragma: no cover - the composite foreign key
            return "The invoice names an offer that does not exist."
        if offer.status is CustomPlanOfferStatus.DECLINED:
            return "The customer declined this custom plan offer after opening the page."
        if offer.status is CustomPlanOfferStatus.CANCELLED:
            return "The platform withdrew this custom plan offer after the page was opened."
        if (
            offer.status is CustomPlanOfferStatus.EXPIRED
            and offer.expires_at is not None
            and invoice.created_at is not None
            and invoice.created_at >= offer.expires_at
        ):
            return "The page was opened after the custom plan offer expired."
        return None

    async def activated(self, invoice: Invoice, *, now: datetime) -> None:
        """Record that a settled invoice made its offer active. Idempotent."""
        if invoice.custom_plan_offer_id is None:
            return
        offer = await self._offers.lock(invoice.custom_plan_offer_id)
        if offer is None or not offer_may_move(offer.status, CustomPlanOfferStatus.ACTIVE):
            return
        previous = offer.status
        offer.status = CustomPlanOfferStatus.ACTIVE
        offer.activated_at = now
        await self._session.flush()
        self._audit.record(
            AuditAction.BILLING_CUSTOM_PLAN_OFFER_ACTIVATED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="custom_plan_offer",
            target_id=offer.id,
            meta={
                "invoice_id": str(invoice.id),
                "plan_version_id": str(offer.plan_version_id),
                "amount": str(invoice.amount_due),
                "currency": invoice.currency,
                "from_status": previous.value,
            },
        )
        logger.info(
            "billing.custom_plan_offer_activated",
            extra={
                "event": "billing.custom_plan_offer_activated",
                "tenant_id": str(self._tenant_id),
                "offer_id": str(offer.id),
                "invoice_id": str(invoice.id),
            },
        )


__all__ = ["CustomPlanOfferLedger"]
