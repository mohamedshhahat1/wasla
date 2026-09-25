"""Custom plans and top-ups under real concurrency (ADR-113, spec 75).

Every race here runs on separate PostgreSQL connections that genuinely commit,
started together at an `asyncio.Barrier`. Nothing is ordered by sleeping: where
one side must wait, it waits on a lock or a unique index, which is exactly the
mechanism under test. Each test states the invariant it proves and asserts it
on the committed rows afterwards.

Because these commit, each builds a workspace of its own with unique plan codes
and removes everything at teardown, the ledger first (it holds its rows by
RESTRICT).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.exceptions import ConflictError, PlanLimitExceededError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    PlanScope,
    PlanVersion,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.campaign import Campaign, CampaignStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.invoice import Invoice, InvoicePurpose, Payment
from app.db.models.membership import Membership
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.db.models.topup import (
    TopupEntitlement,
    TopupProduct,
    TopupPurchase,
    TopupScope,
    TopupStatus,
    TopupValidity,
)
from app.db.models.usage import UsageEvent, UsageEventType
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.plan_admin import PlanCatalogAdmin
from app.platform.topup_admin import TopupAdmin
from app.repositories.campaign_repository import AudienceFilter
from app.schemas.platform_billing import (
    ChangeMode,
    PlanCreate,
    PlanVersionCreate,
    SubscriptionChangePlan,
)
from app.schemas.topup import TopupGrantCreate
from app.services.campaign_service import CampaignService
from app.services.checkout_service import APPLIED, DUPLICATE, REFUSED, CheckoutService
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from app.services.topup_ledger import TopupLedger
from app.services.topup_service import TopupService
from tests.billing_fixtures import erase_ledger
from tests.integration.topup_harness import Paymob, apply, callback

pytestmark = pytest.mark.integration

LIMITS = {
    "agents": 5,
    "period_messages": 10_000,
    "period_ai_turns": 5,
    "period_campaign_messages": 8,
    "storage_bytes": 1024**3,
    "whatsapp_numbers": 1,
    "team_members": 10,
    "knowledge_documents": 50,
}


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", default_plan_code="starter")


@dataclass
class World:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    staff_id: uuid.UUID
    subscription_id: uuid.UUID
    plan_id: uuid.UUID
    ai_product_id: uuid.UUID
    number_product_id: uuid.UUID
    period_end: datetime
    now: datetime


@pytest_asyncio.fixture
async def maker(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=14, max_overflow=6)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[World]:
    now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid.uuid4().hex[:8]
    async with maker() as session:
        tenant = Tenant(name="Race Co", slug=f"race-topup-{tag}")
        owner = User(email=f"race-owner-{tag}@example.com", hashed_password="x", is_active=True)
        staff = User(
            email=f"race-staff-{tag}@example.com",
            hashed_password="x",
            is_active=True,
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        plan = Plan(
            code=f"race-pro-{tag}",
            name="Race Pro",
            price=Decimal("99.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits=dict(LIMITS),
        )
        session.add_all([tenant, owner, staff, plan])
        await session.flush()
        session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=owner.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.ACTIVE,
            )
        )
        version = await PlanCatalog(session).current_version(plan)
        assert version is not None
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            billing_anchor_at=now,
        )
        ai = _product(TopupEntitlement.PERIOD_AI_TURNS, 5, f"race-ai-{tag}")
        numbers = _product(TopupEntitlement.WHATSAPP_NUMBERS, 2, f"race-num-{tag}")
        session.add_all([subscription, ai, numbers])
        await session.commit()
        built = World(
            tenant_id=tenant.id,
            owner_id=owner.id,
            staff_id=staff.id,
            subscription_id=subscription.id,
            plan_id=plan.id,
            ai_product_id=ai.id,
            number_product_id=numbers.id,
            period_end=subscription.current_period_end,
            now=now,
        )
    try:
        yield built
    finally:
        async with maker() as session:
            ids = [built.tenant_id]
            await erase_ledger(session, ids)
            await session.execute(delete(Campaign).where(Campaign.tenant_id == built.tenant_id))
            await session.execute(
                delete(Subscription).where(Subscription.tenant_id == built.tenant_id)
            )
            await session.execute(delete(Plan).where(Plan.tenant_id == built.tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == built.tenant_id))
            await session.execute(
                delete(TopupProduct).where(
                    TopupProduct.id.in_([built.ai_product_id, built.number_product_id])
                )
            )
            await session.execute(delete(Plan).where(Plan.id == built.plan_id))
            await session.execute(delete(User).where(User.id.in_([built.owner_id, built.staff_id])))
            await session.commit()


def _product(key: TopupEntitlement, quantity: int, code: str) -> TopupProduct:
    return TopupProduct(
        code=code,
        name=f"{key.value} +{quantity}",
        entitlement_key=key,
        quantity=quantity,
        price=Decimal("50.00"),
        currency="EGP",
        scope=TopupScope.GLOBAL,
        is_active=True,
        is_public=True,
        validity_policy=TopupValidity.CURRENT_PERIOD_END,
    )


async def _together(*calls: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Start every call at one barrier, each on its own connection."""
    barrier = asyncio.Barrier(len(calls))

    async def run(call: Callable[[], Awaitable[Any]]) -> Any:
        await barrier.wait()
        return await call()

    return list(await asyncio.gather(*(run(call) for call in calls)))


async def _count(maker: async_sessionmaker[AsyncSession], statement: Any) -> int:
    async with maker() as session:
        return int(await session.scalar(statement) or 0)


async def _checkout(
    maker: async_sessionmaker[AsyncSession], world: World, paymob: Paymob
) -> tuple[uuid.UUID, uuid.UUID]:
    """One committed top-up checkout for the AI product: (purchase, payment)."""
    async with maker() as session:
        owner = await session.get(User, world.owner_id)
        checkout = CheckoutService(session, tenant_id=world.tenant_id, provider=paymob.provider())
        started = await TopupService(
            session, tenant_id=world.tenant_id, checkout=checkout
        ).start_checkout(world.ai_product_id, actor=owner, idempotency_key=None, now=world.now)
        await session.commit()
        return started.purchase_id, started.payment_id


async def _limit(
    maker: async_sessionmaker[AsyncSession], world: World, key: LimitKey, *, at: datetime
) -> int | None:
    async with maker() as session:
        entitlement = await EntitlementService(
            session, tenant_id=world.tenant_id, clock=lambda: at
        ).check(key, additional=0)
        return entitlement.limit


def _granted(world: World) -> Any:
    return (
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id == world.tenant_id)
        .where(TopupPurchase.status == TopupStatus.GRANTED)
    )


# ---------------------------------------------------------- callback replays


async def test_the_same_topup_callback_four_times_at_once_grants_once(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 26, 75 (callback x4), TU-02: one delivery applies, three are duplicates,
    one grant, one ledger row, and the limit rises by exactly the quantity.
    """
    paymob = Paymob()
    _, payment_id = await _checkout(maker, world, paymob)
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        assert payment is not None
    signed = callback(payment, transaction=910_000_001)

    async def deliver() -> str:
        async with maker() as session:
            outcome = await apply(
                session, world.tenant_id, paymob.provider(), signed, now=world.now
            )
            await session.commit()
            return outcome

    outcomes = await _together(deliver, deliver, deliver, deliver)

    assert sorted(outcomes) == [APPLIED, DUPLICATE, DUPLICATE, DUPLICATE]
    assert await _count(maker, _granted(world)) == 1
    assert (
        await _count(
            maker,
            select(func.count())
            .select_from(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id),
        )
        == 1
    )
    assert await _limit(maker, world, LimitKey.PERIOD_AI_TURNS, at=world.now) == 5 + 5


async def test_two_workers_granting_one_paid_topup_grant_it_once(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 75 (two workers): the settlement engine asked twice at once about the
    same paid invoice - a callback and a reconciliation, say - grants once.
    The purchase row lock is what orders them.
    """
    paymob = Paymob()
    purchase_id, payment_id = await _checkout(maker, world, paymob)

    async def settle() -> None:
        async with maker() as session:
            payment = await session.get(Payment, payment_id)
            invoice = await session.get(Invoice, payment.invoice_id)  # type: ignore[union-attr]
            assert payment is not None and invoice is not None
            await TopupLedger(session, tenant_id=world.tenant_id).invoice_paid(
                invoice, payment=payment, now=world.now
            )
            await session.commit()

    await _together(settle, settle)

    assert await _count(maker, _granted(world)) == 1
    granted_audits = await _count(
        maker,
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_TOPUP_GRANTED)
        .where(AuditLog.target_id == purchase_id),
    )
    assert granted_audits == 1
    assert await _limit(maker, world, LimitKey.PERIOD_AI_TURNS, at=world.now) == 10


async def test_two_different_successes_on_one_topup_page_grant_once_and_raise(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """A customer charged twice on one page: one grant, and a
    `topup_duplicate_payment` incident for the second charge.
    """
    paymob = Paymob()
    _, payment_id = await _checkout(maker, world, paymob)
    async with maker() as session:
        payment = await session.get(Payment, payment_id)
        assert payment is not None

    def deliver(transaction: int) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                outcome = await apply(
                    session,
                    world.tenant_id,
                    paymob.provider(),
                    callback(payment, transaction=transaction),
                    now=world.now,
                )
                await session.commit()
                return outcome

        return run

    outcomes = await _together(deliver(910_000_101), deliver(910_000_102))

    assert sorted(outcomes) == [APPLIED, REFUSED]
    assert await _count(maker, _granted(world)) == 1
    assert (
        await _count(
            maker,
            select(func.count())
            .select_from(BillingIncident)
            .where(BillingIncident.tenant_id == world.tenant_id)
            .where(BillingIncident.kind == BillingIncidentKind.TOPUP_DUPLICATE_PAYMENT),
        )
        == 1
    )


# ------------------------------------------------------------- idempotency


async def test_one_idempotency_key_four_times_at_once_opens_one_page(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 57, 75 (checkout x4): one purchase, one invoice, one payment, and
    exactly one Paymob intention - the losers never reach the provider.
    """
    paymob = Paymob()

    async def start() -> str:
        async with maker() as session:
            owner = await session.get(User, world.owner_id)
            checkout = CheckoutService(
                session, tenant_id=world.tenant_id, provider=paymob.provider()
            )
            try:
                await TopupService(
                    session, tenant_id=world.tenant_id, checkout=checkout
                ).start_checkout(
                    world.ai_product_id, actor=owner, idempotency_key="same-key", now=world.now
                )
            except ConflictError:
                await session.rollback()
                return "conflict"
            await session.commit()
            return "created"

    outcomes = await _together(start, start, start, start)

    assert sorted(outcomes) == ["conflict", "conflict", "conflict", "created"]
    assert len(paymob.intentions()) == 1
    tenant = world.tenant_id
    assert (
        await _count(
            maker,
            select(func.count())
            .select_from(TopupPurchase)
            .where(TopupPurchase.tenant_id == tenant),
        )
        == 1
    )
    assert (
        await _count(
            maker,
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.tenant_id == tenant)
            .where(Invoice.purpose == InvoicePurpose.TOPUP),
        )
        == 1
    )
    assert (
        await _count(
            maker, select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant)
        )
        == 1
    )


# ------------------------------------------------- top-ups racing consumption


async def test_a_grant_racing_ai_consumption_never_oversells(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 51, 75 (AI usage): twelve turns race a +5 grant on a base of 5.

    Whatever the interleaving, no turn is allowed beyond the limit in force
    when it was counted: recorded turns never exceed 10, every allowed turn
    is recorded, and the allowance left afterwards is exactly the difference.
    """

    async def turn() -> bool:
        async with maker() as session:
            entitlement = await EntitlementService(
                session, tenant_id=world.tenant_id, clock=lambda: world.now
            ).consume(LimitKey.PERIOD_AI_TURNS, event_type=UsageEventType.AI_TURN)
            await session.commit()
            return entitlement.allowed

    async def grant() -> bool:
        async with maker() as session:
            staff = await session.get(User, world.staff_id)
            subscription = await session.get(Subscription, world.subscription_id)
            assert staff is not None and subscription is not None
            await TopupAdmin(session, settings=_settings()).grant(
                world.tenant_id,
                TopupGrantCreate(
                    entitlement_key=TopupEntitlement.PERIOD_AI_TURNS,
                    quantity=5,
                    reason="Race compensation.",
                    expected_subscription_revision=subscription.revision,
                ),
                actor=staff,
                now=world.now,
            )
            await session.commit()
            return True

    results = await _together(*([turn] * 12), grant)
    allowed = sum(1 for result in results[:12] if result)
    recorded = await _count(
        maker,
        select(func.coalesce(func.sum(UsageEvent.quantity), 0))
        .where(UsageEvent.tenant_id == world.tenant_id)
        .where(UsageEvent.event_type == UsageEventType.AI_TURN),
    )

    assert recorded == allowed
    assert 5 <= allowed <= 10
    assert await _limit(maker, world, LimitKey.PERIOD_AI_TURNS, at=world.now) == 10
    # Whatever was left is still there to use, and not one turn more.
    extra = 0
    while True:
        async with maker() as session:
            result = await EntitlementService(
                session, tenant_id=world.tenant_id, clock=lambda: world.now
            ).consume(LimitKey.PERIOD_AI_TURNS, event_type=UsageEventType.AI_TURN)
            await session.commit()
        if not result.allowed:
            break
        extra += 1
    assert allowed + extra == 10


async def test_a_topup_racing_campaign_scheduling_never_over_promises(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 52, 75 (campaigns): three campaigns of four recipients race a +5
    campaign top-up on a base of 8, through the real scheduling path and its
    reservation of other live campaigns' unsent recipients.

    Promised sends never exceed the effective limit in force when each
    campaign was scheduled - at most 13 - so at most three schedule, and a
    third only ever alongside the top-up.
    """
    async with maker() as session:
        account = WhatsAppAccount(
            tenant_id=world.tenant_id,
            phone_number_id=f"race-camp-{uuid.uuid4().hex[:10]}",
            waba_id="race-waba",
            display_phone_number="+201000009999",
        )
        session.add(account)
        await session.flush()
        template = WhatsAppTemplate(
            tenant_id=world.tenant_id,
            account_id=account.id,
            name="race_offer",
            language="ar_EG",
            category=TemplateCategory.MARKETING,
            status=TemplateStatus.APPROVED,
            body_text="Hello.",
            variable_count=0,
        )
        session.add(template)
        for index in range(4):
            contact = Contact(tenant_id=world.tenant_id, wa_id=f"20100000{index:04d}")
            session.add(contact)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=world.tenant_id,
                    contact_id=contact.id,
                    account_id=account.id,
                    status=ConversationStatus.OPEN,
                    mode=ConversationMode.AI,
                    last_inbound_at=datetime.now(UTC),
                )
            )
        await session.flush()
        campaign_ids = []
        service = CampaignService(session=session, tenant_id=world.tenant_id)
        for index in range(3):
            campaign = await service.create(
                account_id=account.id,
                template_id=template.id,
                name=f"Race {index}",
                messages_per_minute=60,
            )
            await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
            campaign_ids.append(campaign.id)
        await session.commit()

    def schedule(campaign_id: uuid.UUID) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                entitlements = EntitlementService(
                    session, tenant_id=world.tenant_id, clock=lambda: world.now
                )
                try:
                    await CampaignService(
                        session=session, tenant_id=world.tenant_id, entitlements=entitlements
                    ).schedule(campaign_id=campaign_id)
                except PlanLimitExceededError:
                    await session.rollback()
                    return "refused"
                await session.commit()
                return "scheduled"

        return run

    async def grant() -> str:
        async with maker() as session:
            staff = await session.get(User, world.staff_id)
            subscription = await session.get(Subscription, world.subscription_id)
            assert staff is not None and subscription is not None
            await TopupAdmin(session, settings=_settings()).grant(
                world.tenant_id,
                TopupGrantCreate(
                    entitlement_key=TopupEntitlement.PERIOD_CAMPAIGN_MESSAGES,
                    quantity=5,
                    reason="Campaign season.",
                    expected_subscription_revision=subscription.revision,
                ),
                actor=staff,
                now=world.now,
            )
            await session.commit()
            return "granted"

    outcomes = await _together(*(schedule(item) for item in campaign_ids), grant)
    scheduled = outcomes.count("scheduled")

    assert "granted" in outcomes
    assert 2 <= scheduled <= 3
    live = await _count(
        maker,
        select(func.count())
        .select_from(Campaign)
        .where(Campaign.tenant_id == world.tenant_id)
        .where(Campaign.status == CampaignStatus.SCHEDULED),
    )
    assert live == scheduled
    assert live * 4 <= 13


async def test_capacity_creation_racing_a_topup_expiry_respects_each_moment(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 75 (expiry vs creation): +2 numbers on a base of 1, expiring at the
    period end. Two creators decide just before it (limit 3) and two just
    after (limit 1), all at once. Each decides under the lock against the
    limit of its own moment: never more than three numbers, and a number is
    added after expiry only while the workspace holds none. Nothing is deleted.
    """
    paymob = Paymob()
    async with maker() as session:
        owner = await session.get(User, world.owner_id)
        checkout = CheckoutService(session, tenant_id=world.tenant_id, provider=paymob.provider())
        started = await TopupService(
            session, tenant_id=world.tenant_id, checkout=checkout
        ).start_checkout(world.number_product_id, actor=owner, idempotency_key=None, now=world.now)
        await session.commit()
    async with maker() as session:
        payment = await session.get(Payment, started.payment_id)
        assert payment is not None
        await apply(
            session,
            world.tenant_id,
            paymob.provider(),
            callback(payment, transaction=910_000_301),
            now=world.now,
        )
        await session.commit()

    before = world.period_end - timedelta(seconds=1)
    after = world.period_end + timedelta(seconds=1)

    def create(at: datetime, label: str) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                try:
                    await EntitlementService(
                        session, tenant_id=world.tenant_id, clock=lambda: at
                    ).reserve_or_refuse(LimitKey.WHATSAPP_NUMBERS)
                except PlanLimitExceededError:
                    await session.rollback()
                    return f"{label}:refused"
                session.add(
                    WhatsAppAccount(
                        tenant_id=world.tenant_id,
                        phone_number_id=f"race-exp-{uuid.uuid4().hex[:12]}",
                        waba_id="race-exp",
                        display_phone_number="+201000008888",
                    )
                )
                await session.commit()
                return f"{label}:created"

        return run

    outcomes = await _together(
        create(before, "before"),
        create(before, "before"),
        create(after, "after"),
        create(after, "after"),
    )
    held = await _count(
        maker,
        select(func.count())
        .select_from(WhatsAppAccount)
        .where(WhatsAppAccount.tenant_id == world.tenant_id),
    )
    assert held == outcomes.count("before:created") + outcomes.count("after:created")
    assert held <= 3
    assert outcomes.count("after:created") <= 1
    assert await _limit(maker, world, LimitKey.WHATSAPP_NUMBERS, at=after) == 1


# ------------------------------------------------------------- custom plans


async def _custom_plan(
    maker: async_sessionmaker[AsyncSession], world: World
) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed custom plan for the race workspace: (plan id, version 1 id)."""
    async with maker() as session:
        staff = await session.get(User, world.staff_id)
        assert staff is not None
        read = await PlanCatalogAdmin(session).create(
            PlanCreate(
                code=f"race-custom-{uuid.uuid4().hex[:6]}",
                name="Race Custom",
                price=Decimal("150.00"),
                currency="EGP",
                interval=BillingInterval.MONTHLY,
                limits=dict(LIMITS),
                scope=PlanScope.TENANT,
                tenant_id=world.tenant_id,
                is_public=False,
                reason="Race terms.",
            ),
            actor=staff,
        )
        await session.commit()
        assert read.current_version is not None
        return read.id, read.current_version.id


async def test_two_assignments_on_one_subscription_revision_one_wins(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 75 (assign vs assign): two operators move the same subscription from
    the same revision at once - one to the custom plan, one to another plan.
    One succeeds, the other is refused 409, and the row says which.
    """
    _, custom_version = await _custom_plan(maker, world)
    async with maker() as session:
        other = Plan(
            code=f"race-other-{uuid.uuid4().hex[:6]}",
            name="Race Other",
            price=Decimal("120.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits=dict(LIMITS),
            tenant_id=None,
        )
        session.add(other)
        await session.flush()
        other_version = await PlanCatalog(session).current_version(other)
        assert other_version is not None
        subscription = await session.get(Subscription, world.subscription_id)
        assert subscription is not None
        revision = subscription.revision
        other_id, other_version_id = other.id, other_version.id
        await session.commit()

    def assign(version_id: uuid.UUID) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                staff = await session.get(User, world.staff_id)
                assert staff is not None
                try:
                    await PlatformBillingOperations(session, settings=_settings()).change_plan(
                        world.subscription_id,
                        SubscriptionChangePlan(
                            plan_version_id=version_id,
                            mode=ChangeMode.NEXT_RENEWAL,
                            reason="Race assignment.",
                            expected_revision=revision,
                        ),
                        actor=staff,
                    )
                except ConflictError:
                    await session.rollback()
                    return "conflict"
                await session.commit()
                return str(version_id)

        return run

    try:
        outcomes = await _together(assign(custom_version), assign(other_version_id))
        assert outcomes.count("conflict") == 1
        winner = next(item for item in outcomes if item != "conflict")
        async with maker() as session:
            subscription = await session.get(Subscription, world.subscription_id)
            assert subscription is not None
            assert str(subscription.scheduled_plan_version_id) == winner
            assert subscription.revision == revision + 1
    finally:
        async with maker() as session:
            await session.execute(
                update(Subscription)
                .where(Subscription.id == world.subscription_id)
                .values(
                    scheduled_plan_version_id=None,
                    scheduled_change_source=None,
                    scheduled_change_reason=None,
                    scheduled_change_actor_id=None,
                    scheduled_change_at=None,
                )
            )
            await session.execute(delete(Plan).where(Plan.id == other_id))
            await session.commit()


async def test_two_publishes_of_one_custom_version_one_wins(
    maker: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Spec 75 (version publish race), CT-03: both publish from version 1 at once.
    One becomes version 2, the other is refused 409, and there is no version 3.
    """
    plan_id, _ = await _custom_plan(maker, world)

    def publish(price: str) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                staff = await session.get(User, world.staff_id)
                assert staff is not None
                try:
                    await PlanCatalogAdmin(session).create_version(
                        plan_id,
                        PlanVersionCreate(
                            price=Decimal(price),
                            currency="EGP",
                            interval=BillingInterval.MONTHLY,
                            limits=dict(LIMITS),
                            expected_version=1,
                            reason="Race publish.",
                        ),
                        actor=staff,
                    )
                except ConflictError:
                    await session.rollback()
                    return "conflict"
                await session.commit()
                return price

        return run

    outcomes = await _together(publish("160.00"), publish("170.00"))

    assert outcomes.count("conflict") == 1
    async with maker() as session:
        versions = (
            await session.scalars(select(PlanVersion.version).where(PlanVersion.plan_id == plan_id))
        ).all()
    assert sorted(versions) == [1, 2]
