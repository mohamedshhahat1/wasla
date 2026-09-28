"""No transaction is open while Paymob is asked for a payment page (DB-008).

The database audit found hosted checkout flushing the invoice and payment,
calling Paymob's Create Intention (a 20 s timeout), and committing only
afterwards: a pooled connection held for the length of somebody else's API
call, and for a custom plan offer the offer's row lock with it, so a decline,
a withdrawal or the expiry sweep waited behind Paymob.

Now the attempt commits first (TX1), Paymob is called with nothing held, and
the order it answers with is bound in a short TX2 under the settlement locks.
Each test looks from *outside*, during the provider call, on its own
connection: is the attempt already durable, is any session of ours sitting in
a transaction, is the offer row free.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    PlanScope,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import PlatformRole
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.checkout import CheckoutRequest, CheckoutSession
from app.integrations.billing.paymob import PaymobProvider
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.schemas.custom_plan import CustomPlanOfferCreate
from app.schemas.platform_billing import PlanCreate
from app.services.checkout_service import CheckoutService
from app.services.custom_plan_offer_service import CustomPlanOfferService
from app.services.plan_catalog import PlanCatalog
from tests.billing_fixtures import add_owner, erase_ledger, price_terms
from tests.paymob_orders import CARD_INTEGRATION_ID, order_from_request

pytestmark = pytest.mark.integration

Probe = Callable[[CheckoutRequest], Awaitable[None]]
PRICE = Decimal("99.00")


class _Paymob(PaymobProvider):
    """The real adapter over a fake socket, running a probe mid-call."""

    def __init__(self, probe: Probe | None = None, *, fail: bool = False) -> None:
        self.probe = probe
        self.fail = fail

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "id": f"pi_test_{uuid.uuid4().hex[:12]}",
                    "client_secret": f"egy_csk_test_{uuid.uuid4().hex[:12]}",
                    "intention_order_id": order_from_request(request),
                },
            )

        super().__init__(
            secret_key="egy_sk_test_boundary",
            public_key="egy_pk_test_boundary",
            hmac_secret="boundary-hmac-synthetic",
            integration_ids=[CARD_INTEGRATION_ID],
            transport=httpx.MockTransport(handler),
        )

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        if self.probe is not None:
            await self.probe(request)
        if self.fail:
            raise httpx.ReadTimeout("synthetic: the provider did not answer")
        return await super().create_checkout(request)


class World:
    """One committed workspace on a 99 EGP plan, with an open renewal."""

    def __init__(self) -> None:
        self.tenant_id = uuid.uuid4()
        self.owner_id = uuid.uuid4()
        self.staff_id = uuid.uuid4()
        self.plan_ids: list[uuid.UUID] = []
        self.invoice_id = uuid.uuid4()


@pytest_asyncio.fixture
async def engine(prepared_database: str) -> AsyncIterator[AsyncEngine]:
    built = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield built
    finally:
        await built.dispose()


@pytest_asyncio.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    built = World()
    now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid.uuid4().hex[:10]
    async with maker() as session:
        tenant = Tenant(id=built.tenant_id, name="Boundary", slug=f"boundary-{tag}")
        staff = User(
            id=built.staff_id,
            email=f"boundary-staff-{tag}@example.com",
            hashed_password="x",
            is_active=True,
            platform_role=PlatformRole.PLATFORM_OWNER,
        )
        plan = Plan(
            code=f"boundary-pro-{tag}",
            name="Pro",
            price=PRICE,
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.AGENTS.value: 5},
        )
        session.add_all([tenant, staff, plan])
        await session.flush()
        built.owner_id = (await add_owner(session, tenant)).id
        built.plan_ids.append(plan.id)
        version = await PlanCatalog(session).current_version(plan)
        assert version is not None
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            plan_price_id=(await price_terms(session, version.id)).get("plan_price_id"),
            status=SubscriptionStatus.PAST_DUE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            billing_anchor_at=now,
            cancel_at_period_end=False,
        )
        session.add(subscription)
        await session.flush()
        session.add(
            Invoice(
                id=built.invoice_id,
                tenant_id=tenant.id,
                subscription_id=subscription.id,
                status=InvoiceStatus.OPEN,
                purpose=InvoicePurpose.RENEWAL,
                plan_code=plan.code,
                plan_version_id=version.id,
                **(await price_terms(session, version.id)),
                amount_due=PRICE,
                amount_paid=Decimal("0.00"),
                currency="EGP",
                period_start=now,
                period_end=now + timedelta(days=30),
                issued_at=now,
                lines=[],
            )
        )
        await session.commit()
    try:
        yield built
    finally:
        async with maker() as session:
            await erase_ledger(session, [built.tenant_id])
            for statement in (
                "DELETE FROM custom_plan_offers WHERE tenant_id = :t",
                "DELETE FROM subscriptions WHERE tenant_id = :t",
                "DELETE FROM audit_logs WHERE tenant_id = :t",
            ):
                await session.execute(text(statement), {"t": built.tenant_id})
            await session.execute(delete(Plan).where(Plan.id.in_(built.plan_ids[1:])))
            await session.execute(delete(Tenant).where(Tenant.id == built.tenant_id))
            await session.execute(delete(Plan).where(Plan.id == built.plan_ids[0]))
            await session.execute(
                text("DELETE FROM audit_logs WHERE actor_id = ANY(:u)"),
                {"u": [built.owner_id, built.staff_id]},
            )
            await session.execute(delete(User).where(User.id.in_([built.owner_id, built.staff_id])))
            await session.commit()


async def _observe(engine: AsyncEngine, reference: str) -> dict[str, object]:
    """What another connection sees while the provider is being asked."""
    async with engine.connect() as connection:
        durable = await connection.scalar(
            text("SELECT count(*) FROM payments WHERE id = :id"), {"id": uuid.UUID(reference)}
        )
        idle_in_transaction = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
            )
        )
    return {"durable": durable, "idle_in_transaction": idle_in_transaction}


async def test_the_provider_is_asked_with_no_transaction_open(
    engine: AsyncEngine, world: World
) -> None:
    seen: dict[str, object] = {}

    async def probe(request: CheckoutRequest) -> None:
        seen.update(await _observe(engine, request.reference))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner = await session.get_one(User, world.owner_id)
        started = await CheckoutService(
            session, tenant_id=world.tenant_id, provider=_Paymob(probe)
        ).start(invoice_id=world.invoice_id, actor=owner)
        await session.commit()

    # TX1 had committed the attempt, and nothing of ours was waiting on Paymob.
    assert seen == {"durable": 1, "idle_in_transaction": 0}
    async with maker() as session:
        payment = await session.get_one(Payment, started.payment_id)
        # TX2 bound what Paymob answered.
        assert payment.provider_order_id is not None
        assert payment.provider_intent_reference is not None


async def test_a_failed_provider_call_keeps_the_attempt_and_frees_the_retry(
    engine: AsyncEngine, world: World
) -> None:
    """A timeout is ambiguous: the attempt stays for reconciliation, the key goes."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner = await session.get_one(User, world.owner_id)
        with pytest.raises(httpx.ReadTimeout):
            await CheckoutService(
                session, tenant_id=world.tenant_id, provider=_Paymob(fail=True)
            ).start(invoice_id=world.invoice_id, actor=owner, idempotency_key="tab-1")
        await session.rollback()

    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, status::text, idempotency_key, provider_order_id, failure_reason"
                    " FROM payments WHERE tenant_id = :t"
                ),
                {"t": world.tenant_id},
            )
        ).all()
    assert len(rows) == 1
    first, status, key, order, reason = rows[0]
    assert (status, key, order) == (PaymentStatus.PENDING.value, None, None)
    assert reason == "The payment page could not be opened."

    # The customer's retry with the same key opens a page instead of a 409.
    async with maker() as session:
        owner = await session.get_one(User, world.owner_id)
        started = await CheckoutService(
            session, tenant_id=world.tenant_id, provider=_Paymob()
        ).start(invoice_id=world.invoice_id, actor=owner, idempotency_key="tab-1")
        await session.commit()
    assert started.payment_id != first


async def _offer(engine: AsyncEngine, world: World) -> uuid.UUID:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        staff = await session.get_one(User, world.staff_id)
        now = datetime.now(UTC).replace(microsecond=0)
        created = await PlanCatalogAdmin(session).create(
            PlanCreate(
                code=f"boundary-abc-{uuid.uuid4().hex[:6]}",
                name="ABC Enterprise",
                price=PRICE,
                currency="EGP",
                interval=BillingInterval.MONTHLY,
                limits={"agents": 9},
                scope=PlanScope.TENANT,
                tenant_id=world.tenant_id,
                is_public=False,
                reason="Negotiated terms.",
            ),
            actor=staff,
            now=now,
        )
        assert created.current_version is not None
        world.plan_ids.append(created.id)
        offer = await PlatformCustomPlanOffers(session).offer(
            world.tenant_id,
            CustomPlanOfferCreate(
                plan_version_id=created.current_version.id, expires_at=None, reason="Deal."
            ),
            actor=staff,
            now=now,
        )
        await session.commit()
        return offer.id


async def test_accepting_an_offer_does_not_hold_the_offer_across_the_provider_call(
    engine: AsyncEngine, world: World
) -> None:
    offer_id = await _offer(engine, world)
    free: list[bool] = []

    async def probe(request: CheckoutRequest) -> None:
        async with engine.connect() as connection:
            # NOWAIT: fails at once if the accepting request still held it.
            await connection.execute(
                text("SELECT 1 FROM custom_plan_offers WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": offer_id},
            )
            free.append(True)
            await connection.rollback()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner = await session.get_one(User, world.owner_id)
        accepted = await CustomPlanOfferService(
            session,
            tenant_id=world.tenant_id,
            checkout=CheckoutService(session, tenant_id=world.tenant_id, provider=_Paymob(probe)),
        ).accept(offer_id, actor=owner, idempotency_key=None)
        await session.commit()
    assert free == [True]
    assert accepted.offer.status.value == "pending_payment"


async def test_an_offer_withdrawn_during_the_provider_call_is_not_accepted(
    engine: AsyncEngine, world: World
) -> None:
    """The lock is gone during the call, so the answer is re-checked after it."""
    offer_id = await _offer(engine, world)

    async def withdraw(request: CheckoutRequest) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE custom_plan_offers SET status = 'cancelled', cancelled_at = now()"
                    " WHERE id = :id"
                ),
                {"id": offer_id},
            )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner = await session.get_one(User, world.owner_id)
        with pytest.raises(ConflictError):
            await CustomPlanOfferService(
                session,
                tenant_id=world.tenant_id,
                checkout=CheckoutService(
                    session, tenant_id=world.tenant_id, provider=_Paymob(withdraw)
                ),
            ).accept(offer_id, actor=owner, idempotency_key=None)
        await session.rollback()
    async with maker() as session:
        status = await session.scalar(
            text("SELECT status::text FROM custom_plan_offers WHERE id = :id"), {"id": offer_id}
        )
    assert status == "cancelled"
