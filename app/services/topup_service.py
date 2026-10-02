"""Top-ups as a workspace sees them: what it may buy, buying it, and what it holds.

The customer half of ADR-113. Buying a top-up is an ordinary hosted checkout -
the same `CheckoutService.open_page`, the same Paymob intention, the same signed
callback and `InvoiceSettlement` - against a `TOPUP` invoice. What makes it a
top-up is the `TopupPurchase` written beside the invoice, which freezes exactly
what the customer was shown and is granted by `TopupLedger` when the money is
confirmed.

The browser names a product id and nothing else. The price, the quantity, the
entitlement, the period and the expiry are read from the database, and a product
this workspace may not see answers 404 exactly like one that does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    TopupNotAvailableError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.telemetry import record_topup_checkout
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import SubscriptionStatus
from app.db.models.channel import Channel
from app.db.models.invoice import InvoicePurpose, InvoiceStatus
from app.db.models.topup import (
    TopupEntitlement,
    TopupProduct,
    TopupPurchase,
    TopupSource,
    TopupStatus,
)
from app.db.models.user import User
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.topup_repository import TopupProductRepository, TopupPurchaseRepository
from app.services.audit_service import AuditTrail
from app.services.checkout_service import CheckoutService
from app.services.entitlement_service import EntitlementService
from app.services.entitlement_terms import term_limit
from app.services.plan_catalog import PlanCatalog
from app.services.topup_ledger import validity_window

logger = get_logger(__name__)

# What a top-up invoice's `plan_code` says. Plan codes cannot contain a colon,
# so no lookup keyed on a plan code can ever match one - a second guard beside
# the explicit purpose checks (ADR-113).
TOPUP_INVOICE_PREFIX: Final = "topup:"
_PLAN_CODE_LENGTH: Final = 50


@dataclass(frozen=True, slots=True)
class StartedTopupCheckout:
    """Where to send the customer, and exactly what they are buying.

    No client secret: it travels inside `redirect_url` only (ADR-044).
    """

    redirect_url: str
    purchase_id: uuid.UUID
    invoice_id: uuid.UUID
    payment_id: uuid.UUID
    amount: Decimal
    currency: str
    entitlement_key: TopupEntitlement
    quantity: int
    expires_at: datetime
    channel_type: Channel | None = None


class TopupService:
    """Top-ups for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        checkout: CheckoutService | None = None,
        clock: Callable[[], datetime] | None = None,
        default_plan_code: str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._checkout = checkout
        # The plan in force decides what is offered (ENT-12, ENT-13), and a
        # workspace with no serving subscription is on the default plan.
        self._default_plan_code = default_plan_code
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._products = TopupProductRepository(session)
        self._purchases = TopupPurchaseRepository(session, tenant_id=tenant_id)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._catalog = PlanCatalog(session)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    # ----------------------------------------------------------------- reads

    async def catalogue(self, *, entitlement: TopupEntitlement | None = None) -> list[TopupProduct]:
        """Active global products and this workspace's own - never another's.

        Only those offered to the plan in force (ENT-13), and no channel slot
        typed for a channel that plan does not include (ENT-12).
        """
        plan_id, channel_types = await self._offer()
        return await self._products.visible_to(
            self._tenant_id,
            entitlement=entitlement,
            plan_id=plan_id,
            channel_types=channel_types,
        )

    async def _offer(self) -> tuple[uuid.UUID | None, frozenset[Channel]]:
        """The plan in force and the channel types it allows: what decides the offer."""
        entitlements = EntitlementService(
            self._session,
            tenant_id=self._tenant_id,
            default_plan_code=self._default_plan_code,
            clock=self._clock,
        )
        plan = await entitlements.plan()
        return (plan.id if plan is not None else None), await entitlements.allowed_channel_types()

    async def history(self, *, limit: int, offset: int) -> tuple[list[TopupPurchase], int]:
        return await self._purchases.history(limit=limit, offset=offset)

    async def active(self) -> list[TopupPurchase]:
        return await self._purchases.active(at=self._clock())

    async def require_purchase(self, purchase_id: uuid.UUID) -> TopupPurchase:
        """One of this workspace's purchases, or a 404 like any other resource."""
        purchase = await self._purchases.get_by_id(purchase_id)
        if purchase is None:
            raise NotFoundError("No such top-up purchase.")
        return purchase

    # -------------------------------------------------------------- checkout

    async def start_checkout(
        self,
        product_id: uuid.UUID,
        *,
        actor: User | None,
        idempotency_key: str | None,
        now: datetime | None = None,
    ) -> StartedTopupCheckout:
        """Freeze a purchase, raise its TOPUP invoice and open a payment page.

        Refusals, all before anything is written or any provider is asked:

        - the same `idempotency_key` again: 409, naming the purchase it already
          opened (the page URL is never stored, so it cannot be replayed);
        - a product this workspace may not see: 404, like one that does not
          exist - another workspace's product, and one not offered to this
          workspace's plan (ENT-13), are indistinguishable;
        - a free product: 409, because a free allowance is a platform grant;
        - no active subscription, or a period already over: 409;
        - a limit the plan already leaves unlimited: 409;
        - a channel slot typed for a channel the plan does not include: 422
          (ENT-12) - a top-up never opens a channel type.
        """
        moment = now if now is not None else self._clock()
        if self._checkout is None or not self._checkout.has_provider:
            raise ValidationError("No payment provider is configured.")
        if idempotency_key:
            existing = await self._purchases.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                raise ConflictError(
                    "A top-up checkout has already been started for this request. "
                    "Read its status rather than starting another.",
                    details={"purchase_id": str(existing.id)},
                )

        product = await self._products.get_by_id(product_id)
        if product is None or not product.visible_to(self._tenant_id):
            raise NotFoundError("No such top-up.")
        plan_id, channel_types = await self._offer()
        if not await self._products.offered_to(product.id, plan_id):
            raise NotFoundError("No such top-up.")
        try:
            await self._refuse(product, now=moment)
        except ConflictError:
            await record_topup_checkout(product.entitlement_key.value, "refused")
            raise
        if product.channel_type is not None and product.channel_type not in channel_types:
            # Asked after the subscription is known to be active, so the types
            # are those of the version it is pinned to.
            await record_topup_checkout(product.entitlement_key.value, "refused")
            raise TopupNotAvailableError()

        subscription = await self._subscriptions.get()
        if subscription is None:  # pragma: no cover - `_refuse` has just checked
            raise ConflictError("Top-ups need an active subscription.")
        # A usage top-up covers the current monthly usage cycle and a capacity
        # top-up the current billing term (ADR-116), frozen here.
        valid_from, valid_until = validity_window(subscription, product.entitlement_key, now=moment)
        try:
            async with self._session.begin_nested():
                invoice = self._invoices.create(
                    subscription_id=subscription.id,
                    plan_code=(TOPUP_INVOICE_PREFIX + product.code)[:_PLAN_CODE_LENGTH],
                    amount_due=product.price,
                    currency=product.currency,
                    period_start=valid_from,
                    period_end=valid_until,
                    lines=[
                        {
                            "kind": "topup",
                            "description": f"{product.name} top-up",
                            "product_code": product.code,
                            "entitlement_key": product.entitlement_key.value,
                            "channel_type": (
                                product.channel_type.value
                                if product.channel_type is not None
                                else None
                            ),
                            "quantity": product.quantity,
                            "amount": str(product.price),
                            "valid_until": valid_until.isoformat(),
                        }
                    ],
                    status=InvoiceStatus.OPEN,
                    purpose=InvoicePurpose.TOPUP,
                )
                invoice.created_at = moment
                await self._session.flush()
                purchase = TopupPurchase(
                    tenant_id=self._tenant_id,
                    topup_product_id=product.id,
                    subscription_id=subscription.id,
                    source=TopupSource.PURCHASE,
                    product_code=product.code,
                    product_name=product.name,
                    entitlement_key=product.entitlement_key,
                    # Frozen with the rest: the slot stays typed as it was sold.
                    channel_type=product.channel_type,
                    quantity=product.quantity,
                    unit_price=product.price,
                    total_amount=product.price,
                    currency=product.currency,
                    billing_period_start=valid_from,
                    billing_period_end=valid_until,
                    expires_at=valid_until,
                    status=TopupStatus.PENDING,
                    invoice_id=invoice.id,
                    idempotency_key=idempotency_key,
                )
                self._session.add(purchase)
                await self._session.flush()
        except IntegrityError:
            if not idempotency_key:
                raise
            # Another request with the same key got here first and committed.
            raise ConflictError(
                "A top-up checkout has already been started for this request. "
                "Read its status rather than starting another."
            ) from None

        try:
            started = await self._checkout.open_page(
                invoice,
                description=f"{product.name} top-up",
                actor=actor,
                idempotency_key=idempotency_key,
            )
        except Exception:
            # The purchase and its invoice were committed before the provider
            # was asked (DB-008). The page may or may not exist, so the
            # purchase stays pending for reconciliation to answer; its key is
            # released so the customer's retry is a new purchase, not a 409.
            await record_topup_checkout(product.entitlement_key.value, "failed")
            unopened = await self._purchases.lock_for_invoice(invoice.id)
            if unopened is not None and unopened.payment_id is None:
                unopened.idempotency_key = None
            await self._session.commit()
            raise
        # Re-read under its lock: the transaction that wrote it has committed.
        locked = await self._purchases.lock_for_invoice(invoice.id)
        if locked is None:  # pragma: no cover - committed with the invoice
            raise NotFoundError("No such top-up.")
        purchase = locked
        purchase.payment_id = started.payment_id
        await self._session.flush()

        self._audit.record(
            AuditAction.BILLING_TOPUP_CHECKOUT_CREATED,
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="topup_purchase",
            target_id=purchase.id,
            target_label=product.code,
            meta={
                "invoice_id": str(invoice.id),
                "payment_id": str(started.payment_id),
                "entitlement_key": product.entitlement_key.value,
                "channel_type": (
                    product.channel_type.value if product.channel_type is not None else None
                ),
                "quantity": product.quantity,
                "amount": str(product.price),
                "currency": product.currency,
                "expires_at": purchase.expires_at.isoformat(),
            },
        )
        await record_topup_checkout(product.entitlement_key.value, "created")
        logger.info(
            "billing.topup_checkout_started",
            extra={
                "event": "billing.topup_checkout_started",
                "tenant_id": str(self._tenant_id),
                "purchase_id": str(purchase.id),
                "entitlement": product.entitlement_key.value,
            },
        )
        return StartedTopupCheckout(
            redirect_url=started.redirect_url,
            purchase_id=purchase.id,
            invoice_id=invoice.id,
            payment_id=started.payment_id,
            amount=started.amount,
            currency=started.currency,
            entitlement_key=purchase.entitlement_key,
            quantity=purchase.quantity,
            expires_at=purchase.expires_at,
            channel_type=purchase.channel_type,
        )

    async def _refuse(self, product: TopupProduct, *, now: datetime) -> None:
        """Why this workspace cannot buy this product now (409), or nothing."""
        if product.price <= 0:
            raise ConflictError(
                "This top-up has no price. Free allowance is granted by the platform."
            )
        subscription = await self._subscriptions.get()
        if subscription is None or subscription.status is not SubscriptionStatus.ACTIVE:
            if subscription is not None and subscription.status is SubscriptionStatus.PAST_DUE:
                raise ConflictError("Settle the open invoice before buying a top-up.")
            raise ConflictError("Top-ups need an active subscription.")
        if subscription.current_period_end <= now:
            raise ConflictError("This billing period has ended. Try again in a moment.")
        terms = await self._catalog.pinned_version(subscription)
        if terms is not None and term_limit(terms, product.entitlement_key.limit_key) is None:
            raise ConflictError("The plan already allows this without limit.")


__all__ = ["TOPUP_INVOICE_PREFIX", "StartedTopupCheckout", "TopupService"]
