"""Custom plan offers as a workspace sees them: read, accept and pay, decline.

The customer half of ADR-114. Accepting an offer is an ordinary hosted checkout
- `CheckoutService.start_offer`, the same Paymob intention, signed callback and
`InvoiceSettlement` as any plan purchase - against a `CHECKOUT` invoice pinned
to the offered version and naming the offer. Nothing about the plan changes
when the owner clicks "Accept & Pay"; it changes when the provider confirms the
money, and only then.

The request names an offer id and, optionally, an idempotency key. The price,
the currency, the billing term and the limits are read from the offer's
immutable price and version (ADR-116) - a yearly offer is paid yearly whatever
the request says; another workspace's offer id is a 404 like one that does not
exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.telemetry import record_custom_plan
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import RESOURCE_LIMITS, LimitKey, Plan, PlanPrice, PlanVersion
from app.db.models.custom_plan_offer import (
    CustomPlanOffer,
    CustomPlanOfferStatus,
    offer_may_move,
)
from app.db.models.user import User
from app.repositories.custom_plan_offer_repository import CustomPlanOfferRepository
from app.schemas.custom_plan import (
    CUSTOM_PLAN_KEYS,
    CustomPlanOfferRead,
    OfferLimitRead,
    OfferPeriodRead,
)
from app.services import billing_calendar
from app.services.audit_service import AuditTrail
from app.services.checkout_service import CheckoutService, StartedCheckout
from app.services.entitlement_terms import ordered, term_channel_types, term_limit

logger = get_logger(__name__)

# What the customer is told about renewals before paying (spec: save card is
# optional). Said on every offer so no client has to invent it.
RENEWAL_NOTE: Final = (
    "Saving your card at checkout is optional. If you save it, each renewal is "
    "charged to it automatically at this price. If you do not, each renewal is "
    "issued as an invoice you pay at checkout."
)


@dataclass(frozen=True, slots=True)
class AcceptedOffer:
    offer: CustomPlanOffer
    version: PlanVersion
    price: PlanPrice
    checkout: StartedCheckout


async def offer_read(
    session: AsyncSession, offer: CustomPlanOffer, *, now: datetime
) -> CustomPlanOfferRead:
    """An offer with the full terms of the version and the price it names."""
    version = await session.get(PlanVersion, offer.plan_version_id)
    plan = await session.get(Plan, offer.plan_id)
    price = await session.get(PlanPrice, offer.plan_price_id)
    if version is None or plan is None or price is None:  # pragma: no cover - RESTRICT
        raise NotFoundError("No such offer.")
    return CustomPlanOfferRead(
        id=offer.id,
        tenant_id=offer.tenant_id,
        status=offer.status.value,
        plan_id=plan.id,
        plan_code=plan.code,
        plan_version_id=version.id,
        version=version.version,
        plan_price_id=price.id,
        billing_interval=price.billing_interval,
        interval_count=price.interval_count,
        name=version.name,
        description=plan.description,
        price=f"{price.amount:.2f}",
        currency=price.currency,
        interval=price.billing_interval,
        limits=[
            OfferLimitRead(
                key=key,
                kind="capacity" if key in RESOURCE_LIMITS else "usage",
                limit=term_limit(version, key),
            )
            for key in CUSTOM_PLAN_KEYS
        ],
        allowed_channel_types=ordered(term_channel_types(version)),
        other_limits={
            key.value: term_limit(version, key) for key in LimitKey if key not in CUSTOM_PLAN_KEYS
        },
        effective_period=OfferPeriodRead(
            interval=price.billing_interval,
            interval_count=price.interval_count,
            if_paid_now_start=now,
            if_paid_now_end=billing_calendar.add_interval(
                now, price.billing_interval, price.interval_count
            ),
        ),
        expires_at=offer.expires_at,
        created_at=offer.created_at,
        accepted_at=offer.accepted_at,
        activated_at=offer.activated_at,
        declined_at=offer.declined_at,
        decline_reason=offer.decline_reason,
        cancelled_at=offer.cancelled_at,
        expired_at=offer.expired_at,
        can_accept=offer.accepting_is_open(now),
        renewal_note=RENEWAL_NOTE,
        revision=offer.revision,
    )


class CustomPlanOfferService:
    """Custom plan offers for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        checkout: CheckoutService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._checkout = checkout
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._offers = CustomPlanOfferRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    def now(self) -> datetime:
        return self._clock()

    async def offers(self) -> list[CustomPlanOffer]:
        """This workspace's offers, newest first - the open one and the history."""
        return await self._offers.history()

    async def open_offer(self) -> CustomPlanOffer | None:
        """The offer awaiting this workspace's decision or payment, if any."""
        return await self._offers.open_offer()

    async def require(self, offer_id: uuid.UUID) -> CustomPlanOffer:
        offer = await self._offers.get_by_id(offer_id)
        if offer is None:
            raise NotFoundError("No such offer.")
        return offer

    async def accept(
        self,
        offer_id: uuid.UUID,
        *,
        actor: User,
        idempotency_key: str | None,
        now: datetime | None = None,
    ) -> AcceptedOffer:
        """Accept an offer and open a payment page for exactly its terms.

        Refusals, before anything is written or any provider is asked: another
        workspace's or a missing offer (404); an offer declined, withdrawn,
        expired or already active (409); a free offer, which is never paid for
        (409 - a free custom plan is assigned by the platform instead).
        """
        moment = now if now is not None else self._clock()
        if self._checkout is None or not self._checkout.has_provider:
            raise ValidationError("No payment provider is configured.")
        offer = await self._offers.lock(offer_id)
        if offer is None:
            raise NotFoundError("No such offer.")
        if not offer.accepting_is_open(moment):
            await record_custom_plan("offer_accept", "refused")
            raise ConflictError(
                "This offer can no longer be accepted.",
                details={"status": offer.status.value},
            )
        version = await self._session.get(PlanVersion, offer.plan_version_id)
        plan = await self._session.get(Plan, offer.plan_id)
        price = await self._session.get(PlanPrice, offer.plan_price_id)
        if version is None or plan is None:  # pragma: no cover - RESTRICT foreign keys
            raise NotFoundError("No such offer.")
        if price is None or version.price <= Decimal("0"):
            raise ConflictError("This offer has no price; the platform assigns it directly.")

        try:
            # The offer's own price, whatever the plan's catalogue says now: a
            # price retired or replaced after the offer was made does not
            # change the terms the customer is accepting (ADR-116).
            started = await self._checkout.start_offer(
                plan=plan,
                version=version,
                price=price,
                offer_id=offer.id,
                actor=actor,
                idempotency_key=idempotency_key,
                now=moment,
            )
        except ConflictError:
            await record_custom_plan("offer_accept", "refused")
            raise

        # The page was opened with the offer lock released (DB-008), so the
        # offer is read again under a fresh lock: a platform withdrawal or the
        # owner's own decline may have landed while the provider was asked.
        # Money paid on that page is held at settlement, as for any page paid
        # after a withdrawal; this request says so rather than accepting.
        relocked = await self._offers.lock(offer.id)
        if relocked is None or not relocked.accepting_is_open(moment):
            await record_custom_plan("offer_accept", "refused")
            raise ConflictError(
                "This offer can no longer be accepted.",
                details={"status": relocked.status.value if relocked else "missing"},
            )
        offer = relocked
        first = offer.status is CustomPlanOfferStatus.OFFERED
        if first and offer_may_move(offer.status, CustomPlanOfferStatus.PENDING_PAYMENT):
            offer.status = CustomPlanOfferStatus.PENDING_PAYMENT
            offer.accepted_at = moment
            offer.accepted_by = actor.id
        await self._session.flush()
        self._audit.record(
            AuditAction.BILLING_CUSTOM_PLAN_OFFER_ACCEPTED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="custom_plan_offer",
            target_id=offer.id,
            target_label=plan.code,
            meta={
                "invoice_id": str(started.invoice_id),
                "payment_id": str(started.payment_id),
                "plan_version_id": str(version.id),
                "plan_price_id": str(price.id),
                "amount": str(started.amount),
                "currency": started.currency,
                "interval": price.billing_interval.value,
                "interval_count": price.interval_count,
                "first_acceptance": first,
            },
        )
        await record_custom_plan("offer_accept", "succeeded")
        logger.info(
            "billing.custom_plan_offer_accepted",
            extra={
                "event": "billing.custom_plan_offer_accepted",
                "tenant_id": str(self._tenant_id),
                "offer_id": str(offer.id),
                "invoice_id": str(started.invoice_id),
            },
        )
        return AcceptedOffer(offer=offer, version=version, price=price, checkout=started)

    async def decline(
        self,
        offer_id: uuid.UUID,
        *,
        actor: User,
        reason: str | None,
        now: datetime | None = None,
    ) -> CustomPlanOffer:
        """Say no. A page opened earlier and paid anyway is refused at settlement."""
        moment = now if now is not None else self._clock()
        offer = await self._offers.lock(offer_id)
        if offer is None:
            raise NotFoundError("No such offer.")
        if not offer_may_move(offer.status, CustomPlanOfferStatus.DECLINED):
            await record_custom_plan("offer_decline", "refused")
            raise ConflictError(
                "This offer can no longer be declined.",
                details={"status": offer.status.value},
            )
        previous = offer.status
        offer.status = CustomPlanOfferStatus.DECLINED
        offer.declined_at = moment
        offer.declined_by = actor.id
        offer.decline_reason = reason
        await self._session.flush()
        self._audit.record(
            AuditAction.BILLING_CUSTOM_PLAN_OFFER_DECLINED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="custom_plan_offer",
            target_id=offer.id,
            meta={"from_status": previous.value, "reason": reason},
        )
        await record_custom_plan("offer_decline", "succeeded")
        return offer


__all__ = [
    "RENEWAL_NOTE",
    "AcceptedOffer",
    "CustomPlanOfferService",
    "offer_read",
]
