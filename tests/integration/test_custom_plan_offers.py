"""Custom plan offers, paid for and renewed, end to end (ADR-114).

Driven through the real services, the real Paymob adapter (signing and
verifying callbacks with its own HMAC), the real settlement engine and the real
billing sweep, against PostgreSQL. Paymob is faked at the socket only.

Proved here:

1. A paid custom plan is **never** the workspace's entitlement before an
   authenticated payment for its offer settles; after it, exactly the offered
   version is pinned and all seven limits apply, once - a replay changes nothing.
2. A declined transaction activates nothing; the offer stays payable.
3. The checkout snapshot wins: a page opened at 1,500 EGP buys version 1 at
   1,500 EGP after the platform has published version 2 at 1,800.
4. Money for an offer declined or withdrawn after its page was opened is held
   with an incident, never granted.
5. An expired offer cannot be accepted, but a page opened in time is honoured.
6. Wrong amount, currency, order, integration or mode: refused, no effect.
7. Renewal: with a saved card, one MOTO/MIT charge at the custom version's
   price, settled once; without one, no charge at all - an invoice the owner pays
   at a hosted checkout. A top-up invoice is never collected by the sweep.
8. A lost callback is recovered by transaction inquiry through the sweep, with
   the documented `auth_token` in the request body.
9. The database refuses an offer of another workspace's plan, an invoice naming
   another workspace's offer or the wrong version, and a second open offer.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    PlanScope,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.custom_plan_offer import CustomPlanOffer, CustomPlanOfferStatus
from app.db.models.enums import PlatformRole
from app.db.models.invoice import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.payment_method import PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement
from app.db.models.user import User
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.repositories.invoice_repository import InvoiceRepository
from app.schemas.custom_plan import CustomPlanOfferCancel, CustomPlanOfferCreate
from app.schemas.platform_billing import (
    ChangeMode,
    PlanCreate,
    PlanVersionCreate,
    SubscriptionChangePlan,
)
from app.services.checkout_service import (
    APPLIED,
    DECLINED,
    DUPLICATE,
    MISMATCHED,
    REFUSED,
    CheckoutService,
    StartedCheckout,
)
from app.services.custom_plan_offer_service import CustomPlanOfferService
from app.services.topup_service import TopupService
from tests.integration.test_billing_remediation_journeys import (
    HMAC_SECRET,
    Paymob,
    _apply,
    _callback,
    _settings,
    _worker,
)
from tests.integration.topup_harness import (
    GIB,
    base_now,
    catalogue,
    later,
    product,
    standing,
    workspace,
)
from tests.payment_tokens import saved_card
from tests.paymob_orders import CARD_INTEGRATION_ID, MOTO_INTEGRATION_ID

pytestmark = pytest.mark.integration

SEVEN: dict[str, int] = {
    "period_messages": 100_000,
    "period_ai_turns": 40_000,
    "period_campaign_messages": 50_000,
    "storage_bytes": 100 * GIB,
    "whatsapp_numbers": 5,
    "team_members": 30,
    "knowledge_documents": 3_000,
}
PRICE = Decimal("1500.00")


async def _staff(session: AsyncSession) -> User:
    user = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    session.add(user)
    await session.flush()
    return user


async def _custom_plan(
    session: AsyncSession, tenant: Tenant, staff: User, *, now: datetime, price: Decimal = PRICE
) -> tuple[uuid.UUID, uuid.UUID]:
    """ABC Enterprise for `tenant`: all seven limits, version 1. Returns (plan, v1)."""
    created = await PlanCatalogAdmin(session).create(
        PlanCreate(
            code=f"abc-{uuid.uuid4().hex[:6]}",
            name="ABC Enterprise",
            price=price,
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 5, **SEVEN},
            scope=PlanScope.TENANT,
            tenant_id=tenant.id,
            is_public=False,
            reason="Negotiated terms.",
        ),
        actor=staff,
        now=now,
    )
    assert created.current_version is not None
    return created.id, created.current_version.id


async def _offer(
    session: AsyncSession,
    tenant: Tenant,
    staff: User,
    version_id: uuid.UUID,
    *,
    now: datetime,
    expires_at: datetime | None = None,
) -> CustomPlanOffer:
    return await PlatformCustomPlanOffers(session).offer(
        tenant.id,
        CustomPlanOfferCreate(
            plan_version_id=version_id, expires_at=expires_at, reason="Enterprise deal."
        ),
        actor=staff,
        now=now,
    )


async def _accept(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    provider: PaymobProvider,
    offer: CustomPlanOffer,
    *,
    now: datetime,
) -> tuple[StartedCheckout, Payment, Invoice]:
    accepted = await CustomPlanOfferService(
        session,
        tenant_id=tenant.id,
        checkout=CheckoutService(session, tenant_id=tenant.id, provider=provider),
    ).accept(offer.id, actor=owner, idempotency_key=None, now=now)
    payment = await session.get(Payment, accepted.checkout.payment_id)
    invoice = await session.get(Invoice, accepted.checkout.invoice_id)
    assert payment is not None and invoice is not None
    return accepted.checkout, payment, invoice


async def _subscription(session: AsyncSession, tenant: Tenant | uuid.UUID) -> Subscription:
    tenant_id = tenant if isinstance(tenant, uuid.UUID) else tenant.id
    row = await session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    assert row is not None
    await session.refresh(row)
    return row


def _signed(payment: Payment, *, transaction: int, **changes: Any) -> tuple[bytes, str]:
    """A signed callback about `payment`, with fields of `obj` replaced."""
    body, _ = _callback(payment, transaction=transaction)
    obj = json.loads(body)["obj"]
    for key, value in changes.items():
        if key == "order_id":
            obj["order"]["id"] = value
        else:
            obj[key] = value
    return (
        json.dumps({"type": "TRANSACTION", "obj": obj}).encode(),
        hmac_signature(obj, secret=HMAC_SECRET),
    )


async def _activations(session: AsyncSession, tenant: Tenant | uuid.UUID) -> int:
    tenant_id = tenant if isinstance(tenant, uuid.UUID) else tenant.id
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .where(AuditLog.action == AuditAction.BILLING_CUSTOM_PLAN_OFFER_ACTIVATED)
        )
        or 0
    )


# ------------------------------------------------------------ 1. activation


async def test_an_offer_activates_exactly_its_version_once_and_only_when_paid(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, name="ABC Company")
    staff = await _staff(db_session)
    paymob = Paymob()
    provider = paymob.provider()
    plan_id, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)

    before = await _subscription(db_session, tenant)
    pro_version = before.plan_version_id
    assert before.plan_id != plan_id, "creating and offering grants nothing"

    started, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)
    # The immutable snapshot: pinned version, its price, a checkout, the offer.
    assert invoice.purpose is InvoicePurpose.CHECKOUT
    assert (invoice.plan_version_id, invoice.amount_due, invoice.currency) == (v1, PRICE, "EGP")
    assert invoice.custom_plan_offer_id == offer.id
    assert (payment.amount, payment.provider_order_id is not None) == (PRICE, True)
    intention = paymob.intentions()[-1]["body"]
    assert intention["amount"] == 150_000
    assert intention["payment_methods"] == [CARD_INTEGRATION_ID], "hosted card, never MOTO"
    assert intention["special_reference"] == str(payment.id)
    await db_session.refresh(offer)
    assert offer.status is CustomPlanOfferStatus.PENDING_PAYMENT
    # Accepting is not paying.
    held = await _subscription(db_session, tenant)
    assert held.plan_version_id == pro_version

    paid_at = later(now, minutes=5)
    signed = _callback(payment, transaction=810_000_001)
    assert await _apply(db_session, tenant.id, provider, signed, now=paid_at) == APPLIED
    await db_session.refresh(invoice)
    await db_session.refresh(payment)
    await db_session.refresh(offer)
    subscription = await _subscription(db_session, tenant)
    assert invoice.status is InvoiceStatus.PAID
    assert payment.status is PaymentStatus.SUCCEEDED
    assert offer.status is CustomPlanOfferStatus.ACTIVE and offer.activated_at == paid_at
    assert (subscription.plan_id, subscription.plan_version_id) == (plan_id, v1)
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.current_period_start == paid_at, "a paid period starts at payment"
    for key, value in SEVEN.items():
        assert (await standing(db_session, tenant, LimitKey(key), at=paid_at)).limit == value
    period = (subscription.current_period_start, subscription.current_period_end)

    # The genuine callback again: nothing moves.
    assert await _apply(db_session, tenant.id, provider, signed, now=later(paid_at, minutes=1)) == (
        DUPLICATE
    )
    subscription = await _subscription(db_session, tenant)
    assert (subscription.current_period_start, subscription.current_period_end) == period
    assert await _activations(db_session, tenant) == 1
    paid_invoices = await db_session.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.status == InvoiceStatus.PAID)
        .where(Invoice.custom_plan_offer_id == offer.id)
    )
    assert paid_invoices == 1


async def test_a_declined_transaction_activates_nothing_and_the_page_stays_payable(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    provider = Paymob().provider()
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    before = (await _subscription(db_session, tenant)).plan_version_id
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)

    declined = _callback(payment, transaction=810_100_001, success=False)
    assert await _apply(db_session, tenant.id, provider, declined, now=now) == DECLINED
    await db_session.refresh(offer)
    await db_session.refresh(invoice)
    assert offer.status is CustomPlanOfferStatus.PENDING_PAYMENT
    assert invoice.status is InvoiceStatus.OPEN
    assert (await _subscription(db_session, tenant)).plan_version_id == before

    retried = _callback(payment, transaction=810_100_002)
    assert await _apply(db_session, tenant.id, provider, retried, now=now) == APPLIED
    await db_session.refresh(offer)
    assert offer.status is CustomPlanOfferStatus.ACTIVE
    assert (await _subscription(db_session, tenant)).plan_version_id == v1


# ------------------------------------------------------------ 3. snapshot


async def test_the_page_opened_at_1500_buys_version_1_after_version_2_at_1800(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    provider = Paymob().provider()
    plan_id, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)

    v2 = await PlanCatalogAdmin(db_session).create_version(
        plan_id,
        PlanVersionCreate(
            price=Decimal("1800.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 5, **SEVEN, "period_ai_turns": 60_000},
            expected_version=1,
            reason="New pricing.",
        ),
        actor=staff,
        now=later(now, minutes=1),
    )
    assert v2.id != v1

    paid_at = later(now, minutes=2)
    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(payment, transaction=810_200_001),
            now=paid_at,
        )
        == APPLIED
    )
    await db_session.refresh(invoice)
    subscription = await _subscription(db_session, tenant)
    assert (invoice.amount_paid, invoice.plan_version_id) == (PRICE, v1)
    assert subscription.plan_version_id == v1
    assert (
        await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=paid_at)
    ).limit == 40_000


# ---------------------------------------------------- 4. declined / withdrawn


@pytest.mark.parametrize("withdrawal", ["declined", "cancelled"])
async def test_money_for_an_offer_declined_or_withdrawn_after_opening_is_held(
    db_session: AsyncSession, withdrawal: str
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    provider = Paymob().provider()
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    before = (await _subscription(db_session, tenant)).plan_version_id
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)

    if withdrawal == "declined":
        await CustomPlanOfferService(db_session, tenant_id=tenant.id).decline(
            offer.id, actor=owner, reason="Too expensive.", now=now
        )
    else:
        await db_session.refresh(offer)
        await PlatformCustomPlanOffers(db_session).cancel(
            offer.id,
            CustomPlanOfferCancel(reason="Terms withdrawn.", expected_revision=offer.revision),
            actor=staff,
            now=now,
        )

    outcome = await _apply(
        db_session, tenant.id, provider, _callback(payment, transaction=810_300_001), now=now
    )
    assert outcome == REFUSED
    await db_session.refresh(offer)
    await db_session.refresh(invoice)
    assert offer.status.value == withdrawal
    assert invoice.status is InvoiceStatus.OPEN
    assert (await _subscription(db_session, tenant)).plan_version_id == before
    incident = await db_session.scalar(
        select(BillingIncident).where(BillingIncident.invoice_id == invoice.id)
    )
    assert incident is not None and incident.kind is BillingIncidentKind.REFUSED_SETTLEMENT

    with pytest.raises(ConflictError):
        await _accept(db_session, tenant, owner, provider, offer, now=now)


# --------------------------------------------------------------- 5. expiry


async def test_an_expired_offer_cannot_be_accepted_but_a_page_opened_in_time_is_honoured(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    provider = paymob.provider()
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now, expires_at=later(now, hours=1))
    _, payment, _ = await _accept(db_session, tenant, owner, provider, offer, now=now)

    worker = _worker(db_session, monkeypatch, provider)
    await worker._expire_offers(now=later(now, hours=2))
    await db_session.refresh(offer)
    assert offer.status is CustomPlanOfferStatus.EXPIRED
    with pytest.raises(ConflictError):
        await _accept(db_session, tenant, owner, provider, offer, now=later(now, hours=2))

    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(payment, transaction=810_400_001),
            now=later(now, hours=3),
        )
        == APPLIED
    )
    await db_session.refresh(offer)
    assert offer.status is CustomPlanOfferStatus.ACTIVE
    assert (await _subscription(db_session, tenant)).plan_version_id == v1


# ------------------------------------------------------- 6. financial binding


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": 180_000},
        {"currency": "USD"},
        {"order_id": 999_999_991},
        {"integration_id": 1_111_111},
        {"is_live": True},
    ],
    ids=["amount", "currency", "order", "integration", "is_live"],
)
async def test_a_callback_that_disagrees_with_the_snapshot_is_refused(
    db_session: AsyncSession, changes: dict[str, Any]
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    provider = Paymob().provider()
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    before = (await _subscription(db_session, tenant)).plan_version_id
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)

    signed = _signed(payment, transaction=810_500_001, **changes)
    event = provider.verify_callback(payload=signed[0], signature=signed[1])
    outcome = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).apply(
        event, now=now
    )
    # An order we never created names no payment at all; everything else is
    # a mismatch on ours.
    assert outcome in (MISMATCHED, "unmatched")
    await db_session.refresh(offer)
    await db_session.refresh(invoice)
    await db_session.refresh(payment)
    assert offer.status is CustomPlanOfferStatus.PENDING_PAYMENT
    assert invoice.status is InvoiceStatus.OPEN and invoice.amount_paid == 0
    assert payment.status is PaymentStatus.PENDING
    assert (await _subscription(db_session, tenant)).plan_version_id == before


# ------------------------------------------------------------- 7. renewals


async def _paid_offer(
    session: AsyncSession, paymob: Paymob, *, now: datetime
) -> tuple[Tenant, User, uuid.UUID, Subscription]:
    await catalogue(session)
    tenant, owner, _ = await workspace(session, now=now)
    staff = await _staff(session)
    provider = paymob.provider()
    _, v1 = await _custom_plan(session, tenant, staff, now=now)
    offer = await _offer(session, tenant, staff, v1, now=now)
    _, payment, _ = await _accept(session, tenant, owner, provider, offer, now=now)
    assert (
        await _apply(
            session, tenant.id, provider, _callback(payment, transaction=810_600_001), now=now
        )
        == APPLIED
    )
    return tenant, owner, v1, await _subscription(session, tenant)


async def test_a_custom_plan_renews_from_a_saved_card_at_its_own_price_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = base_now()
    paymob = Paymob()
    tenant, owner, v1, subscription = await _paid_offer(db_session, paymob, now=now)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    # A top-up checkout left open: the sweep must never collect it (spec 25).
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=10_000)
    await TopupService(
        db_session,
        tenant_id=tenant.id,
        checkout=CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider()),
    ).start_checkout(item.id, actor=owner, idempotency_key=None, now=now)
    await db_session.flush()
    boundary = subscription.current_period_end
    paymob.requests.clear()

    worker = _worker(db_session, monkeypatch, paymob.provider())
    await worker.run_once(now=later(boundary, minutes=1))

    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    assert (renewal.plan_version_id, renewal.amount_due) == (v1, PRICE)
    moto = [
        item
        for item in paymob.intentions()
        if item["body"]["payment_methods"] == [MOTO_INTEGRATION_ID]
    ]
    assert len(moto) == 1 and len(paymob.pays()) == 1
    assert moto[0]["body"]["amount"] == 150_000
    assert paymob.pays()[0]["body"]["payment_token"] == "a-moto-payment-key"
    attempts = (
        await db_session.scalars(select(Payment).where(Payment.is_automatic.is_(True)))
    ).all()
    assert [attempt.invoice_id for attempt in attempts if attempt.tenant_id == tenant.id] == [
        renewal.id
    ], "only the renewal is ever charged; the open top-up is not"

    attempt = next(attempt for attempt in attempts if attempt.tenant_id == tenant.id)
    settled_at = later(boundary, minutes=2)
    assert (
        await _apply(
            db_session,
            tenant.id,
            paymob.provider(),
            _callback(attempt, transaction=810_700_001),
            now=settled_at,
        )
        == APPLIED
    )
    await db_session.refresh(renewal)
    subscription = await _subscription(db_session, tenant)
    assert renewal.status is InvoiceStatus.PAID
    assert subscription.plan_version_id == v1
    assert subscription.current_period_start == boundary
    period = (subscription.current_period_start, subscription.current_period_end)

    await worker.run_once(now=later(boundary, minutes=10))
    subscription = await _subscription(db_session, tenant)
    assert (subscription.current_period_start, subscription.current_period_end) == period
    assert len(paymob.pays()) == 1, "the period advanced exactly once and was charged once"


async def test_a_custom_plan_without_a_saved_card_is_never_charged_at_renewal(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = base_now()
    paymob = Paymob()
    tenant, owner, v1, subscription = await _paid_offer(db_session, paymob, now=now)
    boundary = subscription.current_period_end
    paymob.requests.clear()

    worker = _worker(db_session, monkeypatch, paymob.provider())
    await worker.run_once(now=later(boundary, minutes=1))
    assert paymob.pays() == []
    assert paymob.intentions() == [], "no MOTO intention without a saved card"
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    assert (renewal.status, renewal.amount_due, renewal.plan_version_id) == (
        InvoiceStatus.OPEN,
        PRICE,
        v1,
    )
    assert renewal.issued_at is not None

    # The owner pays it at an ordinary hosted checkout.
    provider = paymob.provider()
    started = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).start(
        invoice_id=renewal.id, actor=owner, now=later(boundary, hours=1)
    )
    assert paymob.intentions()[-1]["body"]["payment_methods"] == [CARD_INTEGRATION_ID]
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None and not payment.is_automatic
    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(payment, transaction=810_800_001),
            now=later(boundary, hours=1),
        )
        == APPLIED
    )
    await db_session.refresh(renewal)
    subscription = await _subscription(db_session, tenant)
    assert renewal.status is InvoiceStatus.PAID
    assert subscription.plan_version_id == v1
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert paymob.pays() == []


# -------------------------------------------------------------- 8. recovery


async def test_a_lost_offer_callback_is_recovered_by_inquiry_through_the_sweep(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    provider = paymob.provider(api_key="an-inquiry-api-key")
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)
    tenant_id = tenant.id
    await db_session.commit()
    await db_session.refresh(payment)
    paymob.inquiry = json.loads(_callback(payment, transaction=810_900_001)[0])["obj"]
    paymob.requests.clear()

    worker = _worker(db_session, monkeypatch, provider)
    await worker._reconcile(now=payment.created_at + timedelta(hours=1))

    await db_session.refresh(payment)
    await db_session.refresh(offer)
    await db_session.refresh(invoice)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID
    assert offer.status is CustomPlanOfferStatus.ACTIVE
    assert (await _subscription(db_session, tenant_id)).plan_version_id == v1
    assert paymob.pays() == [], "recovery asks; it never charges"
    inquiry = next(item for item in paymob.requests if item["url"].endswith("/transaction_inquiry"))
    assert inquiry["body"] == {"auth_token": "inquiry-bearer", "merchant_order_id": str(payment.id)}
    assert await _activations(db_session, tenant_id) == 1


# ------------------------------------------------------------ 9. the schema


async def test_the_database_keeps_offers_and_their_invoices_inside_one_workspace(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    alpha, alpha_owner, _ = await workspace(db_session, now=now, name="Alpha")
    beta, _, beta_subscription = await workspace(db_session, now=now, name="Beta")
    staff = await _staff(db_session)
    provider = Paymob().provider()
    plan_id, v1 = await _custom_plan(db_session, alpha, staff, now=now)
    offer = await _offer(db_session, alpha, staff, v1, now=now)

    # A second open offer for the same workspace.
    with pytest.raises(ConflictError):
        await _offer(db_session, alpha, staff, v1, now=now)
    # Beta cannot be offered Alpha's plan - by the service, and by the table.
    with pytest.raises(ValidationError):
        await _offer(db_session, beta, staff, v1, now=now)
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                CustomPlanOffer(
                    tenant_id=beta.id,
                    plan_id=plan_id,
                    plan_version_id=v1,
                    status=CustomPlanOfferStatus.OFFERED,
                    reason="forged",
                )
            )
            await db_session.flush()
    # An offer's terms are fixed.
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE custom_plan_offers SET tenant_id = :t WHERE id = :o"),
                {"t": beta.id, "o": offer.id},
            )

    _, _, invoice = await _accept(db_session, alpha, alpha_owner, provider, offer, now=now)
    # An invoice of Beta's cannot name Alpha's offer (composite foreign key).
    with pytest.raises(IntegrityError) as refused:
        async with db_session.begin_nested():
            forged = InvoiceRepository(db_session, tenant_id=beta.id).create(
                subscription_id=beta_subscription.id,
                plan_code="forged",
                amount_due=PRICE,
                currency="EGP",
                period_start=later(now, days=1),
                period_end=later(now, days=31),
                lines=[],
                status=InvoiceStatus.OPEN,
                purpose=InvoicePurpose.CHECKOUT,
                plan_version_id=v1,
            )
            forged.custom_plan_offer_id = offer.id
            await db_session.flush()
    assert "fk_invoices_custom_plan_offer_tenant" in str(refused.value) or (
        "custom_plan_not_available_for_workspace" in str(refused.value)
    )  # An invoice naming the offer cannot be re-pointed at another version.
    v2 = await PlanCatalogAdmin(db_session).create_version(
        plan_id,
        PlanVersionCreate(
            price=Decimal("1800.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 5, **SEVEN},
            expected_version=1,
            reason="Version two.",
        ),
        actor=staff,
        now=now,
    )
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE invoices SET plan_version_id = :v WHERE id = :i"),
                {"v": v2.id, "i": invoice.id},
            )


async def test_a_priced_custom_plan_is_offered_never_scheduled_or_free(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    with pytest.raises(ValidationError):
        await PlatformBillingOperations(db_session, settings=_settings()).change_plan(
            subscription.id,
            SubscriptionChangePlan(
                plan_version_id=v1,
                mode=ChangeMode.NEXT_RENEWAL,
                reason="Would bill an unagreed price at renewal.",
                expected_revision=subscription.revision,
            ),
            actor=staff,
            now=now,
        )
    _, free = await _custom_plan(db_session, tenant, staff, now=now, price=Decimal("0.00"))
    with pytest.raises(ValidationError):
        await _offer(db_session, tenant, staff, free, now=now)
