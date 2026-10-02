"""Running top-ups without SQL: the catalogue, purchases, grants and refunds (ADR-113).

Platform staff only, and the same contract as the rest of `/platform/billing`:
every mutation carries a reason, every change to an existing row names the
revision it was based on (409 when stale, under a row lock), and every change
is audited with actor, platform role, reason, before, after and request id.

* **Products** are edited freely for *future* purchases. An existing purchase
  keeps the terms it was bought at - the snapshot is frozen by a trigger - so
  repricing or retiring a product changes nobody who already paid. A product is
  deactivated rather than deleted; deleting is owner-only and refused once any
  purchase names it.
* **A complimentary grant** is a `TopupPurchase` with `source = platform_grant`:
  no invoice, no payment, no price, a reason - the database refuses anything
  else. It expires at the end of the subscription's current period like any
  top-up.
* **Refund review** is the one way a granted top-up is withdrawn. Nothing
  withdraws one automatically; withdrawing deletes nothing and never makes
  usage negative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    TopupNotAvailableError,
    ValidationError,
)
from app.core.telemetry import record_topup_purchase
from app.db.models.audit import AuditAction
from app.db.models.billing import DEFAULT_CURRENCY, LimitKey, Plan, Subscription
from app.db.models.channel import Channel
from app.db.models.invoice import Payment, PaymentStatus
from app.db.models.tenant import Tenant
from app.db.models.topup import (
    PLATFORM_GRANT_NAME,
    TopupEntitlement,
    TopupProduct,
    TopupPurchase,
    TopupScope,
    TopupSource,
    TopupStatus,
    TopupValidity,
)
from app.db.models.user import User
from app.platform.billing_audit import record_platform_billing
from app.repositories.topup_repository import (
    PlatformTopupPurchaseRepository,
    TopupProductRepository,
)
from app.schemas.topup import (
    PlatformTopupProductRead,
    PlatformTopupPurchaseRead,
    TopupGrantCreate,
    TopupProductCreate,
    TopupProductUpdate,
    TopupRefundReview,
)
from app.services.entitlement_service import EntitlementService
from app.services.entitlement_terms import term_channel_types, term_limit
from app.services.plan_catalog import PlanCatalog
from app.services.topup_ledger import TopupLedger, move, purchase_state, validity_window


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


def _product_state(product: TopupProduct, *, eligible: list[str]) -> dict[str, Any]:
    return {
        "code": product.code,
        "name": product.name,
        "entitlement_key": product.entitlement_key.value,
        "channel_type": product.channel_type.value if product.channel_type is not None else None,
        "eligible_plan_codes": eligible,
        "quantity": product.quantity,
        "price": str(product.price),
        "currency": product.currency,
        "scope": product.scope.value,
        "tenant_id": str(product.tenant_id) if product.tenant_id else None,
        "is_active": product.is_active,
        "is_public": product.is_public,
        "revision": product.revision,
    }


class TopupAdmin:
    """The platform operator's top-up catalogue and ledger."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._products = TopupProductRepository(session)
        self._purchases = PlatformTopupPurchaseRepository(session)

    # ------------------------------------------------------------- products

    async def list_products(
        self,
        *,
        scope: TopupScope | None,
        tenant_id: uuid.UUID | None,
        entitlement: TopupEntitlement | None,
        active: bool | None,
        limit: int,
        offset: int,
        channel_type: Channel | None = None,
    ) -> Page[PlatformTopupProductRead]:
        statement = select(TopupProduct)
        if channel_type is not None:
            statement = statement.where(TopupProduct.channel_type == channel_type)
        if scope is not None:
            statement = statement.where(TopupProduct.scope == scope)
        if tenant_id is not None:
            statement = statement.where(TopupProduct.tenant_id == tenant_id)
        if entitlement is not None:
            statement = statement.where(TopupProduct.entitlement_key == entitlement)
        if active is not None:
            statement = statement.where(TopupProduct.is_active.is_(active))
        total = await self._count(statement)
        rows = (
            await self._session.scalars(
                statement.order_by(TopupProduct.entitlement_key, TopupProduct.code)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return Page(items=[await self._read(row) for row in rows], total=total)

    async def get_product(self, product_id: uuid.UUID) -> PlatformTopupProductRead:
        return await self._read(await self._require_product(product_id))

    async def create_product(
        self, payload: TopupProductCreate, *, actor: User
    ) -> PlatformTopupProductRead:
        code = payload.code.strip().lower()
        if await self._products.get_by_code(code) is not None:
            raise ConflictError("A top-up with that code already exists.")
        if payload.tenant_id is not None:
            tenant = await self._session.get(Tenant, payload.tenant_id)
            if tenant is None or tenant.deleted_at is not None:
                raise NotFoundError("No such workspace.")
        eligible = await self._plans_named(payload.eligible_plan_codes or [])
        product = TopupProduct(
            code=code,
            name=payload.name,
            description=payload.description,
            entitlement_key=payload.entitlement_key,
            channel_type=payload.channel_type,
            quantity=payload.quantity,
            price=payload.price,
            currency=payload.currency,
            scope=payload.scope,
            tenant_id=payload.tenant_id,
            is_active=True,
            is_public=payload.is_public,
            validity_policy=TopupValidity.CURRENT_PERIOD_END,
            created_by=actor.id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(product)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("A top-up with that code already exists.") from None
        await self._products.set_eligible_plans(
            product.id, (plan.id for plan in eligible), now=datetime.now(UTC)
        )
        record_platform_billing(
            self._session,
            AuditAction.BILLING_TOPUP_CREATED,
            actor=actor,
            reason=payload.reason,
            target_type="topup_product",
            target_id=product.id,
            tenant_id=product.tenant_id,
            target_label=product.code,
            after=_product_state(product, eligible=sorted(plan.code for plan in eligible)),
        )
        return await self._read(product)

    async def update_product(
        self, product_id: uuid.UUID, payload: TopupProductUpdate, *, actor: User
    ) -> PlatformTopupProductRead:
        """Terms and presentation for *new* purchases; existing ones are frozen."""
        product = await self._lock_product(product_id, expected_revision=payload.expected_revision)
        named = payload.model_fields_set
        if (
            "channel_type" in named
            and payload.channel_type is not None
            and product.entitlement_key.limit_key is not LimitKey.CHANNEL_CONNECTIONS
        ):
            raise ValidationError("Only a channel_connections top-up is typed for a channel.")
        eligible = (
            await self._plans_named(payload.eligible_plan_codes or [])
            if "eligible_plan_codes" in named
            else None
        )
        before = _product_state(
            product, eligible=await self._products.eligible_plan_codes(product.id)
        )
        if payload.name is not None:
            product.name = payload.name
        if payload.description is not None:
            product.description = payload.description
        if payload.quantity is not None:
            product.quantity = payload.quantity
        if payload.price is not None:
            product.price = payload.price
        if payload.is_public is not None:
            product.is_public = payload.is_public
        if "channel_type" in named:
            product.channel_type = payload.channel_type
        await self._session.flush()
        if eligible is not None:
            await self._products.set_eligible_plans(
                product.id, (plan.id for plan in eligible), now=datetime.now(UTC)
            )
        record_platform_billing(
            self._session,
            AuditAction.BILLING_TOPUP_UPDATED,
            actor=actor,
            reason=payload.reason,
            target_type="topup_product",
            target_id=product.id,
            tenant_id=product.tenant_id,
            target_label=product.code,
            before=before,
            after=_product_state(
                product, eligible=await self._products.eligible_plan_codes(product.id)
            ),
        )
        return await self._read(product)

    async def set_active(
        self,
        product_id: uuid.UUID,
        *,
        active: bool,
        expected_revision: int,
        reason: str,
        actor: User,
    ) -> PlatformTopupProductRead:
        """Offer a product for new purchases, or stop. Nobody who bought it changes."""
        product = await self._lock_product(product_id, expected_revision=expected_revision)
        if product.is_active is active:
            raise ConflictError(f"The top-up is already {'active' if active else 'inactive'}.")
        eligible = await self._products.eligible_plan_codes(product.id)
        before = _product_state(product, eligible=eligible)
        product.is_active = active
        await self._session.flush()
        record_platform_billing(
            self._session,
            (
                AuditAction.BILLING_TOPUP_ACTIVATED
                if active
                else AuditAction.BILLING_TOPUP_DEACTIVATED
            ),
            actor=actor,
            reason=reason,
            target_type="topup_product",
            target_id=product.id,
            tenant_id=product.tenant_id,
            target_label=product.code,
            before=before,
            after=_product_state(product, eligible=eligible),
        )
        return await self._read(product)

    async def delete_product(self, product_id: uuid.UUID, *, reason: str, actor: User) -> None:
        """Hard-delete a product nobody ever bought. Platform owner only (route)."""
        product = await self._lock_product(product_id)
        if await self._products.purchase_count(product.id):
            raise ConflictError(
                "This top-up has purchases, which are financial history. Deactivate it instead."
            )
        snapshot = _product_state(
            product, eligible=await self._products.eligible_plan_codes(product.id)
        )
        await self._session.delete(product)
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_TOPUP_DELETED,
            actor=actor,
            reason=reason,
            target_type="topup_product",
            target_id=product_id,
            tenant_id=product.tenant_id,
            target_label=snapshot["code"],
            before=snapshot,
        )

    # ------------------------------------------------------------ purchases

    async def list_purchases(
        self,
        *,
        tenant_id: uuid.UUID | None,
        status: TopupStatus | None,
        source: TopupSource | None,
        entitlement: TopupEntitlement | None,
        limit: int,
        offset: int,
    ) -> Page[PlatformTopupPurchaseRead]:
        statement = select(TopupPurchase)
        if tenant_id is not None:
            statement = statement.where(TopupPurchase.tenant_id == tenant_id)
        if status is not None:
            statement = statement.where(TopupPurchase.status == status)
        if source is not None:
            statement = statement.where(TopupPurchase.source == source)
        if entitlement is not None:
            statement = statement.where(TopupPurchase.entitlement_key == entitlement)
        total = await self._count(statement)
        rows = (
            await self._session.scalars(
                statement.order_by(TopupPurchase.created_at.desc(), TopupPurchase.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return Page(items=[await self.read_purchase(row) for row in rows], total=total)

    async def get_purchase(self, purchase_id: uuid.UUID) -> PlatformTopupPurchaseRead:
        purchase = await self._purchases.get_by_id(purchase_id)
        if purchase is None:
            raise NotFoundError("No such top-up purchase.")
        return await self.read_purchase(purchase)

    async def read_purchase(self, purchase: TopupPurchase) -> PlatformTopupPurchaseRead:
        status: PaymentStatus | None = None
        if purchase.payment_id is not None:
            payment = await self._session.get(Payment, purchase.payment_id)
            status = payment.status if payment is not None else None
        return PlatformTopupPurchaseRead.build(purchase, payment_status=status)

    async def grant(
        self,
        tenant_id: uuid.UUID,
        payload: TopupGrantCreate,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlatformTopupPurchaseRead:
        """A complimentary top-up, until the end of the current period (ADR-113).

        Recorded as what it is: `source = platform_grant`, no invoice, no
        payment, no price. Refused for a limit the plan already leaves
        unlimited, and for a subscription that is not serving - there is no
        period to grant it in.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._session.scalar(
            select(Subscription).where(Subscription.tenant_id == tenant_id).with_for_update()
        )
        if subscription is None:
            raise NotFoundError("This workspace has no subscription.")
        await self._session.refresh(subscription)
        if subscription.revision != payload.expected_subscription_revision:
            raise ConflictError(
                f"The subscription has changed (revision {subscription.revision}); "
                "reload it and retry."
            )
        if not subscription.is_serving:
            raise ConflictError("The workspace's subscription is not active.")
        if subscription.current_period_end <= moment:
            raise ConflictError("The current billing period has ended. Try again in a moment.")
        key = payload.entitlement_key.limit_key
        terms = await PlanCatalog(self._session).pinned_version(subscription)
        if terms is not None and term_limit(terms, key) is None:
            raise ConflictError("The plan already allows this without limit.")
        if payload.channel_type is not None and payload.channel_type not in term_channel_types(
            terms
        ):
            # A grant never opens a channel type either (ENT-12).
            raise TopupNotAvailableError(
                "This workspace's plan does not include that channel, so a slot for it "
                "could not be used."
            )

        entitlements = EntitlementService(
            self._session,
            tenant_id=tenant_id,
            default_plan_code=self._settings.default_plan_code,
            clock=lambda: moment,
        )
        before = await entitlements.check(key, additional=0)
        # The same window a purchase would cover (ADR-116): a usage grant lasts
        # the current monthly cycle, a capacity grant the billing term.
        valid_from, valid_until = validity_window(subscription, payload.entitlement_key, now=moment)
        purchase = TopupPurchase(
            tenant_id=tenant_id,
            topup_product_id=None,
            subscription_id=subscription.id,
            source=TopupSource.PLATFORM_GRANT,
            product_code=None,
            product_name=PLATFORM_GRANT_NAME,
            entitlement_key=payload.entitlement_key,
            channel_type=payload.channel_type,
            quantity=payload.quantity,
            unit_price=Decimal("0.00"),
            total_amount=Decimal("0.00"),
            currency=DEFAULT_CURRENCY,
            billing_period_start=valid_from,
            billing_period_end=valid_until,
            expires_at=valid_until,
            status=TopupStatus.PENDING,
            reason=payload.reason,
            actor_id=actor.id,
        )
        self._session.add(purchase)
        await self._session.flush()
        await TopupLedger(self._session, tenant_id=tenant_id).grant(purchase, now=moment)
        after = await entitlements.check(key, additional=0)
        record_platform_billing(
            self._session,
            AuditAction.BILLING_TOPUP_PLATFORM_GRANTED,
            actor=actor,
            reason=payload.reason,
            target_type="topup_purchase",
            target_id=purchase.id,
            tenant_id=tenant_id,
            target_label=payload.entitlement_key.value,
            before={"effective_limit": before.limit, "platform_grant_limit": before.grant_limit},
            after={"effective_limit": after.limit, "platform_grant_limit": after.grant_limit},
            extra={
                "entitlement_key": payload.entitlement_key.value,
                "channel_type": (
                    payload.channel_type.value if payload.channel_type is not None else None
                ),
                "quantity": payload.quantity,
                "expires_at": purchase.expires_at.isoformat(),
                "valid_until": payload.valid_until,
            },
        )
        return await self.read_purchase(purchase)

    async def review_refund(
        self,
        purchase_id: uuid.UUID,
        payload: TopupRefundReview,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlatformTopupPurchaseRead:
        """Decide a refunded, granted top-up: keep it, or withdraw it from now on.

        Withdrawing removes the quantity from the effective limit and nothing
        else. A capacity the workspace is using stays in use (it is simply over
        its limit), and usage already recorded is never reduced.
        """
        moment = now if now is not None else datetime.now(UTC)
        purchase = await self._purchases.lock(purchase_id)
        if purchase is None:
            raise NotFoundError("No such top-up purchase.")
        if purchase.revision != payload.expected_revision:
            raise ConflictError(
                f"The purchase has changed (revision {purchase.revision}); reload it and retry."
            )
        if purchase.status is not TopupStatus.REFUND_REVIEW:
            raise ConflictError("Only a refunded top-up awaiting review can be decided.")

        entitlements = EntitlementService(
            self._session,
            tenant_id=purchase.tenant_id,
            default_plan_code=self._settings.default_plan_code,
            clock=lambda: moment,
        )
        effective_before = (await entitlements.check(purchase.limit_key, additional=0)).limit
        before = purchase_state(purchase)
        if payload.decision == "keep":
            move(purchase, TopupStatus.GRANTED)
        else:
            move(purchase, TopupStatus.CANCELLED)
            purchase.ended_at = moment
        await self._session.flush()
        effective_after = (await entitlements.check(purchase.limit_key, additional=0)).limit
        record_platform_billing(
            self._session,
            AuditAction.BILLING_TOPUP_REFUND_REVIEWED,
            actor=actor,
            reason=payload.reason,
            target_type="topup_purchase",
            target_id=purchase.id,
            tenant_id=purchase.tenant_id,
            target_label=purchase.product_code,
            before={**before, "effective_limit": effective_before},
            after={**purchase_state(purchase), "effective_limit": effective_after},
            extra={"decision": payload.decision},
        )
        await record_topup_purchase(
            purchase.entitlement_key.value, "kept" if payload.decision == "keep" else "withdrawn"
        )
        return await self.read_purchase(purchase)

    # --------------------------------------------------------------- helpers

    async def _read(self, product: TopupProduct) -> PlatformTopupProductRead:
        # Reloaded, not read lazily: a flush expires the server-maintained
        # `updated_at`, and an async session cannot load it on attribute access.
        await self._session.refresh(product)
        return PlatformTopupProductRead.build(
            product,
            purchases=await self._products.purchase_count(product.id),
            eligible_plan_codes=await self._products.eligible_plan_codes(product.id),
        )

    async def _plans_named(self, codes: list[str]) -> list[Plan]:
        """The plans `codes` name, every one of them, or a 422 naming those that do not exist."""
        if not codes:
            return []
        plans = list(await self._session.scalars(select(Plan).where(Plan.code.in_(codes))))
        missing = sorted(set(codes) - {plan.code for plan in plans})
        if missing:
            raise ValidationError(
                "No such plan: " + ", ".join(missing) + ".", details={"plan_codes": missing}
            )
        return plans

    async def _count(self, statement: Select[Any]) -> int:
        return int(
            await self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )

    async def _require_product(self, product_id: uuid.UUID) -> TopupProduct:
        product = await self._products.get_by_id(product_id)
        if product is None:
            raise NotFoundError("No such top-up.")
        return product

    async def _lock_product(
        self, product_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> TopupProduct:
        product = await self._session.scalar(
            select(TopupProduct).where(TopupProduct.id == product_id).with_for_update()
        )
        if product is None:
            raise NotFoundError("No such top-up.")
        await self._session.refresh(product)
        if expected_revision is not None and product.revision != expected_revision:
            raise ConflictError(
                f"The top-up has changed (revision {product.revision}); reload it and retry."
            )
        return product


__all__ = ["Page", "TopupAdmin"]
