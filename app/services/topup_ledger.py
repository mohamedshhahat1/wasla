"""What money arriving, leaving or being withdrawn does to a top-up (ADR-113).

The settlement engine's top-up half. `InvoiceSettlement` decides whether a TOPUP
invoice may take money exactly as it does for every other invoice; once it has,
this module decides what the workspace gets. Three events, and the rule for each:

* **Paid** - the invoice's one purchase (`uq_topup_purchases_invoice_id`) is
  locked and, if it is still pending, granted: its quantity joins the effective
  limit until the `expires_at` frozen at checkout. A purchase is granted at most
  once because it is one row moving out of `PENDING` under a row lock; a replayed
  callback never reaches here at all, because the invoice is already paid. When
  granting would be wrong - the period it was bought for has ended, or the
  subscription has stopped serving - the money is recorded (`PAID`) and a
  `topup_paid_but_not_granted` incident asks an operator to refund it.
* **Reversed** - a refund before the grant cancels the purchase; nothing was
  ever added. A refund *after* the grant never subtracts anything
  automatically: the purchase moves to `REFUND_REVIEW`, where it still counts,
  and an incident asks an operator whether to withdraw it. Withdrawing a
  capacity never deletes anything, and withdrawing usage never makes usage
  negative - the workspace is simply over its limit.
* **Voided** - an abandoned top-up invoice cancels its pending purchase.

Nothing here talks to a provider, touches a plan or moves money.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.telemetry import record_topup_grant, record_topup_purchase
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing_incident import BillingIncidentKind
from app.db.models.invoice import Invoice, Payment
from app.db.models.topup import TopupPurchase, TopupSource, TopupStatus, topup_may_move
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.topup_repository import TopupPurchaseRepository
from app.services.audit_service import AuditTrail
from app.services.billing_incident_service import raise_incident
from app.services.entitlement_service import EntitlementService, hold_limit_lock

logger = get_logger(__name__)

# Why a paid top-up was not granted, in the words the incident carries.
PERIOD_ENDED: Final = "The billing period this top-up was bought for had already ended."
NOT_SERVING: Final = "The workspace's subscription is no longer active."


def purchase_state(purchase: TopupPurchase) -> dict[str, Any]:
    """A purchase as an audit entry records it: commercial fields, no secrets."""
    return {
        "status": purchase.status.value,
        "source": purchase.source.value,
        "entitlement_key": purchase.entitlement_key.value,
        "quantity": purchase.quantity,
        "total_amount": str(purchase.total_amount),
        "currency": purchase.currency,
        "expires_at": purchase.expires_at.isoformat(),
        "granted_at": purchase.granted_at.isoformat() if purchase.granted_at else None,
        "revision": purchase.revision,
    }


def move(purchase: TopupPurchase, target: TopupStatus) -> None:
    """The only way a purchase changes status: through `TOPUP_TRANSITIONS`."""
    if not topup_may_move(purchase.status, target):
        raise ConflictError(f"A top-up cannot go from {purchase.status.value} to {target.value}.")
    purchase.status = target


class TopupLedger:
    """Grants, reversals and cancellations for one workspace's top-ups."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._purchases = TopupPurchaseRepository(session, tenant_id=tenant_id)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    # ------------------------------------------------------------ paid

    async def invoice_paid(self, invoice: Invoice, *, payment: Payment, now: datetime) -> None:
        """A TOPUP invoice has just become paid: grant its purchase, once."""
        purchase = await self._purchases.lock_for_invoice(invoice.id)
        if purchase is None:
            # The checkout path writes the purchase with the invoice, in one
            # savepoint; reaching this is a state nothing should produce.
            await raise_incident(
                self._session,
                kind=BillingIncidentKind.TOPUP_UNKNOWN_CALLBACK,
                dedupe_key=str(invoice.id),
                tenant_id=self._tenant_id,
                payment_id=payment.id,
                invoice_id=invoice.id,
                provider=payment.provider,
                provider_transaction_id=payment.provider_reference,
                amount=payment.amount,
                currency=payment.currency,
                detail="A top-up invoice was paid and no purchase names it.",
                now=now,
            )
            return
        if purchase.status is not TopupStatus.PENDING:
            # Decided already. Unreachable through settlement, which refuses a
            # paid invoice before it gets here; kept so this method is safe to
            # call twice by construction rather than by its caller's care.
            return

        purchase.paid_at = now
        if purchase.payment_id is None:
            purchase.payment_id = payment.id
        refusal = await self._grant_refusal(purchase, now=now)
        self._audit.record(
            AuditAction.BILLING_TOPUP_PAYMENT_SETTLED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="topup_purchase",
            target_id=purchase.id,
            target_label=purchase.product_code,
            meta={
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": payment.currency,
                "entitlement_key": purchase.entitlement_key.value,
            },
        )
        await record_topup_purchase(purchase.entitlement_key.value, "settled")
        if refusal is None:
            await self.grant(purchase, now=now)
            return

        move(purchase, TopupStatus.PAID)
        await raise_incident(
            self._session,
            kind=BillingIncidentKind.TOPUP_PAID_BUT_NOT_GRANTED,
            dedupe_key=str(purchase.id),
            tenant_id=self._tenant_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            provider=payment.provider,
            provider_transaction_id=payment.provider_reference,
            amount=payment.amount,
            currency=payment.currency,
            detail=refusal,
            now=now,
        )
        await record_topup_grant(
            purchase.entitlement_key.value, purchase.source.value, "not_granted"
        )
        logger.warning(
            "billing.topup_not_granted",
            extra={
                "event": "billing.topup_not_granted",
                "tenant_id": str(self._tenant_id),
                "purchase_id": str(purchase.id),
                "reason": refusal,
            },
        )

    async def _grant_refusal(self, purchase: TopupPurchase, *, now: datetime) -> str | None:
        """Why this paid purchase cannot be granted now, or None to grant it."""
        if now >= purchase.expires_at:
            return PERIOD_ENDED
        subscription = await self._subscriptions.get()
        if subscription is None or not subscription.is_serving:
            return NOT_SERVING
        return None

    async def grant(self, purchase: TopupPurchase, *, now: datetime) -> None:
        """Make a purchase's quantity live, under the workspace's lock for its key.

        Called for a settled purchase and for a platform grant alike. The move
        out of `PENDING` is the once-only step; the advisory lock orders it
        against consumption of the same allowance. A purchase is audited here;
        a platform grant is audited by the platform layer, with the operator's
        role, reason and request id.
        """
        await hold_limit_lock(self._session, tenant_id=self._tenant_id, key=purchase.limit_key)
        move(purchase, TopupStatus.GRANTED)
        purchase.granted_at = now
        await self._session.flush()
        if purchase.source is TopupSource.PURCHASE:
            self._audit.record(
                AuditAction.BILLING_TOPUP_GRANTED,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="topup_purchase",
                target_id=purchase.id,
                target_label=purchase.product_code,
                meta={
                    "entitlement_key": purchase.entitlement_key.value,
                    "quantity": purchase.quantity,
                    "expires_at": purchase.expires_at.isoformat(),
                },
            )
        await record_topup_grant(purchase.entitlement_key.value, purchase.source.value, "granted")
        logger.info(
            "billing.topup_granted",
            extra={
                "event": "billing.topup_granted",
                "tenant_id": str(self._tenant_id),
                "purchase_id": str(purchase.id),
                "entitlement": purchase.entitlement_key.value,
                "source": purchase.source.value,
            },
        )

    # ------------------------------------------------------------ reversed

    async def invoice_reversed(
        self,
        invoice: Invoice,
        *,
        payment: Payment,
        full: bool,
        operator_requested: bool,
        now: datetime,
    ) -> None:
        """Money for a TOPUP invoice went back. Never subtracts on its own.

        An operator's *partial* refund is goodwill: the purchase is untouched.
        Anything else - a full refund, or a reversal nobody here asked for -
        cancels a purchase that was never granted, and sends a granted one to
        review, where it keeps counting until an operator decides.
        """
        purchase = await self._purchases.lock_for_invoice(invoice.id)
        if purchase is None:
            return
        if operator_requested and not full:
            return

        before = purchase_state(purchase)
        if purchase.status in (TopupStatus.PENDING, TopupStatus.PAID):
            if not full:
                return
            move(purchase, TopupStatus.CANCELLED)
            purchase.ended_at = now
            self._audit.record(
                AuditAction.BILLING_TOPUP_CANCELLED,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="topup_purchase",
                target_id=purchase.id,
                target_label=purchase.product_code,
                meta={"reason": "refunded_before_grant", "before": before},
            )
            await record_topup_purchase(purchase.entitlement_key.value, "cancelled")
            return

        if purchase.status not in (TopupStatus.GRANTED, TopupStatus.EXPIRED):
            return  # Already under review, or already cancelled.

        consumed = await self._consumed_beyond_base(purchase)
        if purchase.status is TopupStatus.GRANTED:
            move(purchase, TopupStatus.REFUND_REVIEW)
            await record_topup_purchase(purchase.entitlement_key.value, "refund_review")
        kind = (
            BillingIncidentKind.TOPUP_REFUND_AFTER_CONSUMPTION
            if consumed or purchase.status is TopupStatus.EXPIRED
            else BillingIncidentKind.TOPUP_ENTITLEMENT_REVERSAL_BLOCKED
        )
        await raise_incident(
            self._session,
            kind=kind,
            dedupe_key=f"{purchase.id}:{payment.refunded_amount}",
            tenant_id=self._tenant_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            provider=payment.provider,
            provider_transaction_id=payment.provider_reference,
            amount=payment.refunded_amount,
            currency=payment.currency,
            detail=(
                f"A granted top-up ({purchase.quantity} {purchase.entitlement_key.value}) was "
                f"refunded; {consumed} of it was already in use. Nothing was withdrawn - "
                f"decide under /platform/billing/topup-purchases/{purchase.id}/refund-review."
            ),
            now=now,
        )

    async def _consumed_beyond_base(self, purchase: TopupPurchase) -> int:
        """How much of the allowance above the plan's own is already in use.

        What decides which incident a refund raises. For a usage key it is the
        period's consumption beyond the plan's allowance; for a capacity, what
        is held beyond it. Either way the answer never withdraws anything.
        """
        entitlement = await EntitlementService(self._session, tenant_id=self._tenant_id).check(
            purchase.limit_key, additional=0
        )
        if entitlement.base_limit is None:
            return 0
        return min(max(entitlement.used - entitlement.base_limit, 0), purchase.quantity)

    # ------------------------------------------------------------ voided

    async def invoice_voided(self, invoice: Invoice, *, now: datetime) -> None:
        """An abandoned top-up invoice was withdrawn: its purchase never happens."""
        purchase = await self._purchases.lock_for_invoice(invoice.id)
        if purchase is None or purchase.status is not TopupStatus.PENDING:
            return
        move(purchase, TopupStatus.CANCELLED)
        purchase.ended_at = now
        self._audit.record(
            AuditAction.BILLING_TOPUP_CANCELLED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="topup_purchase",
            target_id=purchase.id,
            target_label=purchase.product_code,
            meta={"reason": "invoice_voided", "invoice_id": str(invoice.id)},
        )
        await record_topup_purchase(purchase.entitlement_key.value, "cancelled")


__all__ = ["NOT_SERVING", "PERIOD_ENDED", "TopupLedger", "move", "purchase_state"]
