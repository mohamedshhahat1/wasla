"""Channel top-ups: general and typed slots, plan eligibility, the ENT-10 money path.

ENT-10..ENT-13 (ADR-131). Every purchase goes through the real `TopupService`,
`CheckoutService`, the real Paymob adapter's signature verification,
`InvoiceSettlement` and `TopupLedger`, with Paymob faked at the socket only
(`topup_harness.Paymob`). Connections are made through the real
`ChannelConnectionService` and the real capacity guard, under a registry that
operates the synthetic channels the way the adapter stage will.

What is proved:

- **General slots** add to the plan's: Starter's one connection plus a bought
  +2 seats three connections across channels, and the fourth is 409.
- **Typed slots** (ENT-11) serve only their own channel, and take that channel
  first: a typed Instagram slot never seats a WhatsApp number, and an Instagram
  connection still fits when every general slot is full.
- **A top-up never opens a channel type** (ENT-12): a typed slot for a channel
  the plan does not include is not listed, is refused at checkout (422) and as a
  platform grant (422).
- **Eligibility** (ENT-13): a product offered to other plans is invisible - not
  listed, and 404 at checkout - and a free plan buys one it is offered.
- **The money path is the number top-up's, unchanged** (ENT-10): a callback
  replayed grants once, a declined payment grants nothing, money after the term
  is held and raised, a refund before the grant cancels and one after it goes
  to review while still counting, a yearly term's slot lasts the year, a grant
  is never a sale, and a purchase keeps its snapshot - channel type included -
  whatever the product becomes.
- **The control plane** states and audits a product's channel type and plans.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError as SchemaError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.exceptions import NotFoundError, TopupNotAvailableError, ValidationError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import BillingInterval, LimitKey, Plan, Subscription
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.channel import Channel
from app.db.models.enums import PlatformRole
from app.db.models.invoice import Invoice, InvoicePurpose, Payment
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.user import User
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.platform.plan_admin import PlanCatalogAdmin
from app.platform.topup_admin import TopupAdmin
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.schemas.platform_billing import PlanPriceCreate
from app.schemas.topup import (
    TopupGrantCreate,
    TopupProductCreate,
    TopupProductUpdate,
    TopupRefundReview,
)
from app.services.billing_calendar import current_usage_period
from app.services.channel_capacity import ChannelCapacityExceededError, ChannelCapacityGuard
from app.services.channel_connection_service import ChannelConnectionService
from app.services.checkout_service import APPLIED, DECLINED, DUPLICATE, CheckoutService
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from app.services.topup_service import TopupService
from tests.channel_fakes import SyntheticAdapter
from tests.integration.plan_catalogue import own_plan
from tests.integration.topup_harness import (
    Paymob,
    apply,
    base_now,
    buy,
    callback,
    later,
    product,
    standing,
    workspace,
)

pytestmark = pytest.mark.integration

CHANNELS = TopupEntitlement.CHANNEL_CONNECTIONS
SLOTS = LimitKey.CHANNEL_CONNECTIONS
# Placeholder terms (ENT-20) of this suite's own: three channels allowed, and
# Telegram and TikTok left out so a slot for them is one the plan cannot use.
TYPES = ["whatsapp", "instagram", "messenger"]


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", default_plan_code="starter")


async def _catalogue(session: AsyncSession) -> tuple[Plan, Plan]:
    """Starter (free, one connection) and Pro (99 EGP, three), both on TYPES."""
    starter = await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"channel_connections": 1, "period_ai_turns": 100},
        allowed_channel_types=TYPES,
    )
    pro = await own_plan(
        session,
        code="pro",
        price=Decimal("99.00"),
        limits={"channel_connections": 3, "period_ai_turns": 5_000},
        allowed_channel_types=TYPES,
    )
    return starter, pro


def _registry() -> ChannelRegistry:
    adapters: dict[Channel, ChannelAdapter] = {Channel.WHATSAPP: WhatsAppAdapter()}
    for channel in (Channel.INSTAGRAM, Channel.MESSENGER, Channel.TELEGRAM, Channel.TIKTOK):
        adapters[channel] = cast(ChannelAdapter, SyntheticAdapter(channel))
    return ChannelRegistry(adapters)


def _neutral(session: AsyncSession, tenant: Tenant) -> ChannelConnectionService:
    return ChannelConnectionService(
        session, tenant_id=tenant.id, default_plan_code="starter", registry=_registry()
    )


async def _number(session: AsyncSession, tenant: Tenant) -> None:
    """One WhatsApp number, through the guard exactly as the connect route asks it."""
    slot = await ChannelCapacityGuard(
        session, tenant_id=tenant.id, default_plan_code="starter"
    ).reserve_or_refuse(Channel.WHATSAPP)
    await WhatsAppAccountRepository(session, tenant_id=tenant.id).connect(
        phone_number_id=f"1{uuid.uuid4().int % 10**11:011d}",
        waba_id="waba-topups",
        display_phone_number="+20 100 000 0009",
        slot=slot,
    )


async def _bought(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    paymob: Paymob,
    *,
    quantity: int,
    channel_type: Channel | None = None,
    eligible: tuple[Plan, ...] = (),
    now: datetime,
    transaction: int,
) -> TopupPurchase:
    """A channel top-up bought and paid, through the real checkout and callback."""
    item = await product(
        session,
        entitlement=CHANNELS,
        quantity=quantity,
        price="150.00",
        channel_type=channel_type,
        eligible=eligible,
    )
    started, payment = await buy(session, tenant, owner, paymob, item, now=now)
    assert started.channel_type is channel_type
    signed = callback(payment, transaction=transaction)
    assert await apply(session, tenant.id, paymob.provider(), signed, now=now) == APPLIED
    purchase = await session.get(TopupPurchase, started.purchase_id)
    assert purchase is not None
    await session.refresh(purchase)
    return purchase


async def _staff(session: AsyncSession) -> User:
    user = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    session.add(user)
    await session.flush()
    return user


def _service(session: AsyncSession, tenant: Tenant, paymob: Paymob) -> TopupService:
    return TopupService(
        session,
        tenant_id=tenant.id,
        checkout=CheckoutService(session, tenant_id=tenant.id, provider=paymob.provider()),
        default_plan_code="starter",
    )


# ------------------------------------------------------------- general slots


async def test_a_general_topup_on_starter_seats_three_channels_and_refuses_a_fourth(
    db_session: AsyncSession,
) -> None:
    """Section 17's example: included 1 + purchased 2 = 3 / 3 across three channels."""
    now = base_now()
    starter, _ = await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="starter")
    paymob = Paymob()
    purchase = await _bought(
        db_session,
        tenant,
        owner,
        paymob,
        quantity=2,
        eligible=(starter,),
        now=now,
        transaction=930_000_001,
    )
    assert (purchase.status, purchase.channel_type) == (TopupStatus.GRANTED, None)

    await _number(db_session, tenant)
    neutral = _neutral(db_session, tenant)
    await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-1", actor=owner)
    await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-1", actor=owner)
    with pytest.raises(ChannelCapacityExceededError):
        await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-2", actor=owner)
    with pytest.raises(ChannelCapacityExceededError):
        await _number(db_session, tenant)

    state = await standing(db_session, tenant, SLOTS, at=now)
    assert (state.base_limit, state.topup_limit, state.grant_limit, state.limit) == (1, 2, 0, 3)
    assert (state.used, state.remaining, state.over_limit) == (3, 0, False)
    assert state.capacity is not None
    assert state.capacity.active == {
        Channel.WHATSAPP: 1,
        Channel.INSTAGRAM: 1,
        Channel.MESSENGER: 1,
    }


# --------------------------------------------------------------- typed slots


async def test_a_typed_instagram_slot_never_seats_a_whatsapp_number(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    typed = await _bought(
        db_session,
        tenant,
        owner,
        paymob,
        quantity=1,
        channel_type=Channel.INSTAGRAM,
        now=now,
        transaction=930_000_101,
    )
    assert typed.channel_type is Channel.INSTAGRAM

    for _ in range(3):
        await _number(db_session, tenant)
    with pytest.raises(ChannelCapacityExceededError) as fourth:
        await _number(db_session, tenant)
    assert fourth.value.details is not None
    assert fourth.value.details["typed_capacity"] == {"instagram": 1}
    # The typed slot is there - for Instagram.
    await _neutral(db_session, tenant).connect(
        channel=Channel.INSTAGRAM, external_account_id="ig-typed", actor=owner
    )
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert state.capacity is not None
    assert (state.limit, state.used, state.remaining) == (4, 4, 0)
    assert state.capacity.typed_used(Channel.INSTAGRAM) == 1


async def test_an_unused_typed_slot_takes_its_channel_when_general_slots_are_full(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    await _bought(
        db_session,
        tenant,
        owner,
        paymob,
        quantity=1,
        channel_type=Channel.INSTAGRAM,
        now=now,
        transaction=930_000_201,
    )
    neutral = _neutral(db_session, tenant)
    await _number(db_session, tenant)
    await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-1", actor=owner)
    await _number(db_session, tenant)
    # Every general slot is taken; the Instagram connection takes its own.
    await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-1", actor=owner)
    with pytest.raises(ChannelCapacityExceededError):
        await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-2", actor=owner)
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert state.capacity is not None
    assert state.capacity.overflow() == 3
    assert state.capacity.fits()


# ---------------------------------------- ENT-12: never opens a channel type


async def test_a_slot_for_a_channel_the_plan_does_not_include_is_never_sold_or_granted(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    tiktok = await product(
        db_session, entitlement=CHANNELS, quantity=1, price="150.00", channel_type=Channel.TIKTOK
    )
    instagram = await product(
        db_session,
        entitlement=CHANNELS,
        quantity=1,
        price="150.00",
        channel_type=Channel.INSTAGRAM,
    )
    service = _service(db_session, tenant, paymob)

    listed = {item.id for item in await service.catalogue(entitlement=CHANNELS)}
    assert instagram.id in listed
    assert tiktok.id not in listed
    with pytest.raises(TopupNotAvailableError) as refused:
        await service.start_checkout(tiktok.id, actor=owner, idempotency_key=None, now=now)
    assert (refused.value.status_code, refused.value.error_code) == (422, "topup_not_available")
    assert paymob.intentions() == [], "Paymob is never asked for a slot the plan cannot use"
    purchases = await db_session.scalar(
        select(func.count()).select_from(TopupPurchase).where(TopupPurchase.tenant_id == tenant.id)
    )
    assert purchases == 0

    staff = await _staff(db_session)
    admin = TopupAdmin(db_session, settings=_settings())
    with pytest.raises(TopupNotAvailableError):
        await admin.grant(
            tenant.id,
            TopupGrantCreate(
                entitlement_key=CHANNELS,
                quantity=1,
                channel_type=Channel.TIKTOK,
                reason="Goodwill for a launch.",
                expected_subscription_revision=subscription.revision,
            ),
            actor=staff,
        )
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert (state.topup_limit, state.grant_limit) == (0, 0)


# ------------------------------------------------------------ ENT-13: plans


async def test_a_product_offered_to_other_plans_is_invisible(db_session: AsyncSession) -> None:
    now = base_now()
    starter, _ = await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    starter_only = await product(
        db_session, entitlement=CHANNELS, quantity=2, price="150.00", eligible=(starter,)
    )
    everyone = await product(db_session, entitlement=CHANNELS, quantity=1, price="150.00")
    service = _service(db_session, tenant, paymob)

    listed = {item.id for item in await service.catalogue()}
    assert everyone.id in listed
    assert starter_only.id not in listed
    with pytest.raises(NotFoundError) as hidden:
        await service.start_checkout(starter_only.id, actor=owner, idempotency_key=None, now=now)
    assert hidden.value.message == "No such top-up."
    assert paymob.intentions() == []


# ------------------------------------------------- ENT-10: the money path


async def test_a_replayed_callback_grants_the_slots_once(db_session: AsyncSession) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    item = await product(db_session, entitlement=CHANNELS, quantity=2, price="150.00")
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None and invoice.purpose is InvoicePurpose.TOPUP
    assert invoice.lines[0]["entitlement_key"] == "channel_connections"
    assert invoice.lines[0]["channel_type"] is None
    signed = callback(payment, transaction=930_000_301)

    assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == APPLIED
    for _ in range(4):
        assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == DUPLICATE

    state = await standing(db_session, tenant, SLOTS, at=now)
    assert (state.base_limit, state.topup_limit, state.limit) == (3, 2, 5)
    granted = await db_session.scalar(
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id == tenant.id, TopupPurchase.status == TopupStatus.GRANTED)
    )
    assert granted == 1


async def test_a_declined_payment_grants_no_slot(db_session: AsyncSession) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    item = await product(
        db_session, entitlement=CHANNELS, quantity=2, price="150.00", channel_type=Channel.MESSENGER
    )
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    declined = callback(payment, transaction=930_000_401, success=False)

    assert await apply(db_session, tenant.id, paymob.provider(), declined, now=now) == DECLINED

    purchase = await db_session.get(TopupPurchase, started.purchase_id)
    assert purchase is not None
    await db_session.refresh(purchase)
    assert (purchase.status, purchase.granted_at) == (TopupStatus.PENDING, None)
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert (state.limit, state.topup_limit) == (3, 0)


async def test_money_after_the_term_is_held_and_raised(db_session: AsyncSession) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    item = await product(
        db_session, entitlement=CHANNELS, quantity=1, price="150.00", channel_type=Channel.INSTAGRAM
    )
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    late = later(subscription.current_period_end, minutes=5)

    signed = callback(payment, transaction=930_000_501)
    assert await apply(db_session, tenant.id, paymob.provider(), signed, now=late) == APPLIED

    purchase = await db_session.get(TopupPurchase, started.purchase_id)
    assert purchase is not None
    await db_session.refresh(purchase)
    assert (purchase.status, purchase.granted_at) == (TopupStatus.PAID, None)
    incident = await db_session.scalar(
        select(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
        .where(BillingIncident.kind == BillingIncidentKind.TOPUP_PAID_BUT_NOT_GRANTED)
    )
    assert incident is not None
    assert (await standing(db_session, tenant, SLOTS, at=now)).topup_limit == 0


async def test_refunds_cancel_before_the_grant_and_go_to_review_after_it(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()

    # Before the grant: paid after the term, so held, then refunded in full.
    early = await product(db_session, entitlement=CHANNELS, quantity=1, price="150.00")
    started, payment = await buy(db_session, tenant, owner, paymob, early, now=now)
    late = later(subscription.current_period_end, minutes=5)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=930_000_601),
        now=late,
    )
    await db_session.refresh(payment)
    refund = callback(payment, transaction=930_000_601, refunded_cents=15_000)
    assert await apply(db_session, tenant.id, paymob.provider(), refund, now=late) == APPLIED
    cancelled = await db_session.get(TopupPurchase, started.purchase_id)
    assert cancelled is not None
    await db_session.refresh(cancelled)
    assert (cancelled.status, cancelled.granted_at) == (TopupStatus.CANCELLED, None)

    # After the grant: refunded, sent to review, and still counting meanwhile.
    granted = await _bought(
        db_session,
        tenant,
        owner,
        paymob,
        quantity=2,
        channel_type=Channel.MESSENGER,
        now=now,
        transaction=930_000_602,
    )
    payment_row = await db_session.get(Payment, granted.payment_id)
    assert payment_row is not None
    refund = callback(payment_row, transaction=930_000_602, refunded_cents=15_000)
    assert await apply(db_session, tenant.id, paymob.provider(), refund, now=now) == APPLIED
    await db_session.refresh(granted)
    assert granted.status is TopupStatus.REFUND_REVIEW
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert state.capacity is not None
    assert state.capacity.typed_purchased == {Channel.MESSENGER: 2}

    staff = await _staff(db_session)
    await TopupAdmin(db_session, settings=_settings()).review_refund(
        granted.id,
        TopupRefundReview(
            decision="withdraw", reason="Refunded in full.", expected_revision=granted.revision
        ),
        actor=staff,
        now=now,
    )
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert state.capacity is not None
    assert state.capacity.typed_purchased == {}


async def test_a_platform_grant_of_typed_slots_is_never_a_sale(db_session: AsyncSession) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, _owner, subscription = await workspace(db_session, now=now, plan_code="pro")
    staff = await _staff(db_session)

    read = await TopupAdmin(db_session, settings=_settings()).grant(
        tenant.id,
        TopupGrantCreate(
            entitlement_key=CHANNELS,
            quantity=2,
            channel_type=Channel.INSTAGRAM,
            reason="Launch goodwill.",
            expected_subscription_revision=subscription.revision,
        ),
        actor=staff,
        now=now,
    )

    assert (read.source, read.channel_type, read.amount) == (
        TopupSource.PLATFORM_GRANT,
        Channel.INSTAGRAM,
        "0.00",
    )
    assert (read.invoice_id, read.payment_id) == (None, None)
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert (state.topup_limit, state.grant_limit, state.limit) == (0, 2, 5)
    assert state.capacity is not None
    assert state.capacity.typed_granted == {Channel.INSTAGRAM: 2}
    assert state.capacity.typed_purchased == {}
    entry = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant.id)
        .where(AuditLog.action == AuditAction.BILLING_TOPUP_PLATFORM_GRANTED)
    )
    assert entry is not None and entry.meta is not None
    assert entry.meta["channel_type"] == "instagram"


async def test_a_purchase_keeps_its_snapshot_whatever_the_product_becomes(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    paymob = Paymob()
    item = await product(
        db_session, entitlement=CHANNELS, quantity=1, price="150.00", channel_type=Channel.INSTAGRAM
    )
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    staff = await _staff(db_session)
    await db_session.refresh(item)
    await TopupAdmin(db_session, settings=_settings()).update_product(
        item.id,
        TopupProductUpdate(
            quantity=5,
            price=Decimal("999.00"),
            channel_type=None,
            expected_revision=item.revision,
            reason="Repriced and made general.",
        ),
        actor=staff,
    )
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=930_000_801),
        now=now,
    )

    purchase = await db_session.get(TopupPurchase, started.purchase_id)
    assert purchase is not None
    await db_session.refresh(purchase)
    assert (purchase.quantity, purchase.total_amount, purchase.channel_type) == (
        1,
        Decimal("150.00"),
        Channel.INSTAGRAM,
    )
    state = await standing(db_session, tenant, SLOTS, at=now)
    assert state.capacity is not None
    assert state.capacity.typed_purchased == {Channel.INSTAGRAM: 1}
    await db_session.refresh(item)
    assert (item.quantity, item.channel_type) == (5, None)


async def _yearly(session: AsyncSession, tenant: Tenant, *, now: datetime) -> Subscription:
    """Put the workspace on Pro's yearly price, as a settled annual purchase does (ADR-116)."""
    staff = await _staff(session)
    plan = await session.scalar(select(Plan).where(Plan.code == "pro"))
    assert plan is not None
    catalog = PlanCatalog(session)
    version = await catalog.current_version(plan, at=now)
    assert version is not None
    price = await catalog.price_for_term(version, interval=BillingInterval.YEARLY)
    if price is None:
        created = await PlanCatalogAdmin(session).create_price(
            version.id,
            PlanPriceCreate(
                billing_interval=BillingInterval.YEARLY,
                amount=Decimal("990.00"),
                reason="Synthetic yearly test price.",
            ),
            actor=staff,
            now=now - timedelta(days=1),
        )
        price = await catalog.get_price(created.id)
    subscription, _ = await SubscriptionService(session, tenant_id=tenant.id).apply_purchase(
        version=version, price=price, now=now
    )
    return subscription


async def test_a_yearly_terms_slot_lasts_the_year_not_the_month(db_session: AsyncSession) -> None:
    """A capacity key is valid until the end of the billing term (ADR-116), here a year.

    M-E18's killer. The term and the usage cycle must differ for that to mean
    anything, so it is asserted first: a year of term, a month of cycle.
    """
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    subscription = await _yearly(db_session, tenant, now=now)
    term = subscription.current_period_end - subscription.current_period_start
    cycle_start, cycle_end = current_usage_period(subscription, now)
    assert term >= timedelta(days=365)
    assert cycle_end - cycle_start <= timedelta(days=31)
    paymob = Paymob()
    bought_at = now + timedelta(days=40)

    purchase = await _bought(
        db_session,
        tenant,
        owner,
        paymob,
        quantity=1,
        channel_type=Channel.MESSENGER,
        now=bought_at,
        transaction=930_000_901,
    )

    assert purchase.expires_at == subscription.current_period_end, "the paid year"
    assert purchase.expires_at > current_usage_period(subscription, bought_at)[1]
    in_month_eight = now + timedelta(days=230)
    assert (await standing(db_session, tenant, SLOTS, at=in_month_eight)).topup_limit == 1
    after = subscription.current_period_end + timedelta(minutes=1)
    assert (await standing(db_session, tenant, SLOTS, at=after)).topup_limit == 0


# ------------------------------------------------------------ control plane


async def test_staff_state_a_products_channel_and_plans_and_every_change_is_audited(
    db_session: AsyncSession,
) -> None:
    starter, pro = await _catalogue(db_session)
    staff = await _staff(db_session)
    admin = TopupAdmin(db_session, settings=_settings())
    code = f"ig-slot-{uuid.uuid4().hex[:6]}"

    created = await admin.create_product(
        TopupProductCreate(
            code=code,
            name="Instagram slot",
            entitlement_key=CHANNELS,
            quantity=1,
            price=Decimal("120.00"),
            currency="EGP",
            channel_type=Channel.INSTAGRAM,
            eligible_plan_codes=["pro", "starter"],
            reason="Typed slot for launch.",
        ),
        actor=staff,
    )
    assert created.channel_type is Channel.INSTAGRAM
    assert created.eligible_plan_codes == sorted([starter.code, pro.code])

    updated = await admin.update_product(
        created.id,
        TopupProductUpdate(
            channel_type=None,
            eligible_plan_codes=[],
            expected_revision=created.revision,
            reason="Any channel, any plan.",
        ),
        actor=staff,
    )
    assert (updated.channel_type, updated.eligible_plan_codes) == (None, [])
    entries = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.target_id == created.id).order_by(AuditLog.occurred_at)
        )
    )
    actions = [entry.action for entry in entries]
    assert actions == [AuditAction.BILLING_TOPUP_CREATED, AuditAction.BILLING_TOPUP_UPDATED]
    change: dict[str, Any] = entries[1].meta or {}
    assert change["before"]["channel_type"] == "instagram"
    assert change["before"]["eligible_plan_codes"] == sorted([starter.code, pro.code])
    assert change["after"]["channel_type"] is None
    assert change["after"]["eligible_plan_codes"] == []

    with pytest.raises(ValidationError) as unknown:
        await admin.create_product(
            TopupProductCreate(
                code=f"x-{uuid.uuid4().hex[:6]}",
                name="Nowhere",
                entitlement_key=CHANNELS,
                quantity=1,
                price=Decimal("10.00"),
                currency="EGP",
                eligible_plan_codes=["no-such-plan"],
                reason="Bad plan code.",
            ),
            actor=staff,
        )
    assert unknown.value.details == {"plan_codes": ["no-such-plan"]}


def test_the_schemas_refuse_a_typed_slot_on_another_key_and_the_retired_key() -> None:
    base: dict[str, Any] = {
        "code": "bad-typed",
        "name": "Bad",
        "quantity": 1,
        "price": "10.00",
        "currency": "EGP",
        "reason": "Testing refusals.",
    }
    with pytest.raises(SchemaError, match="Only a channel_connections top-up is typed"):
        TopupProductCreate(
            **base, entitlement_key=TopupEntitlement.PERIOD_AI_TURNS, channel_type=Channel.INSTAGRAM
        )
    with pytest.raises(SchemaError, match="whatsapp_numbers is retired"):
        TopupProductCreate(**base, entitlement_key=TopupEntitlement.WHATSAPP_NUMBERS)
    with pytest.raises(SchemaError, match="whatsapp_numbers is retired"):
        TopupGrantCreate(
            entitlement_key=TopupEntitlement.WHATSAPP_NUMBERS,
            quantity=1,
            reason="Testing refusals.",
            expected_subscription_revision=1,
        )
    with pytest.raises(SchemaError, match="Each eligible plan is named once"):
        TopupProductCreate(**base, entitlement_key=CHANNELS, eligible_plan_codes=["pro", "pro"])


# -------------------------------------------------------------- isolation


async def test_another_workspaces_typed_product_is_invisible_and_404(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro", name="Ours")
    other, _, _ = await workspace(db_session, now=now, plan_code="pro", name="Theirs")
    paymob = Paymob()
    theirs = await product(
        db_session,
        entitlement=CHANNELS,
        quantity=1,
        price="150.00",
        channel_type=Channel.INSTAGRAM,
        tenant=other,
    )
    service = _service(db_session, tenant, paymob)

    assert theirs.id not in {item.id for item in await service.catalogue()}
    with pytest.raises(NotFoundError):
        await service.start_checkout(theirs.id, actor=owner, idempotency_key=None, now=now)
    assert paymob.intentions() == []
    purchases = await db_session.scalar(
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id.in_([tenant.id, other.id]))
    )
    assert purchases == 0
