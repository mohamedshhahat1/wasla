"""Two settlements of one invoice at once: exactly one applies (DB-001, DB-003).

The database audit reproduced a customer charged twice for one bill with no
trace: two payment pages for one open invoice, both paid, both callbacks
applied concurrently - two succeeded payments worth 198.00, an invoice saying
99.00 was paid, and no incident. Settled one after the other, the second was
correctly refused and raised as a `duplicate_payment` incident. Only the
concurrent path was wrong: nothing locked the invoice. The same two payments
on *one* payment row deadlocked instead, because the payment, invoice and
subscription rows were taken in whatever order a flush wrote them.

Every scenario here runs `RUNS` times against real PostgreSQL, each racer on
its own connection, really committing. Half the runs hold every racer at a
barrier just before it takes its first settlement lock - the widest window
there is, since no racer holds anything yet - and half start them together
with no barrier. No sleeps.

What every run must end with, read by SQL independent of the application:

- exactly one settlement applied its money, whoever reported it;
- `amount_paid` equals the net of the applied payments and never exceeds the
  invoice (the audit's DB-F03);
- every other collected payment is *held*, with a billing incident - so the
  applied money plus the held money is all the money a provider reported;
- one `payment_recorded` entry, one subscription advance, one offer
  activation, one top-up grant;
- no racer failed: no deadlock, no serialization failure, no 500.

`test_the_database_refuses_a_settlement_that_skipped_the_lock` removes the
application's lock and proves the database's own backstop (migration 0074)
turns the double settlement into a refused commit instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.exceptions import WaslaError
from app.db.errors import sqlstate
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
from app.db.models.topup import TopupEntitlement, TopupProduct, TopupScope, TopupValidity
from app.db.models.user import User
from app.integrations.billing.checkout import ChargeInquiry, InquiryVerdict
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.platform.platform_billing import PlatformBillingService
from app.schemas.custom_plan import CustomPlanOfferCreate
from app.schemas.platform_billing import ManualPaymentCreate, PlanCreate
from app.services.checkout_service import APPLIED, REFUSED, CheckoutService
from app.services.custom_plan_offer_service import CustomPlanOfferService
from app.services.payment_reconciliation_service import PaymentReconciler
from app.services.plan_catalog import PlanCatalog
from app.services.settlement_service import InvoiceSettlement
from app.services.topup_service import TopupService
from tests.billing_fixtures import add_owner, erase_ledger, price_terms
from tests.paymob_orders import CARD_INTEGRATION_ID, order_for, order_from_request

pytestmark = pytest.mark.integration

RUNS = 10
HMAC_SECRET = "settlement-race-hmac-synthetic"
PRICE = Decimal("99.00")
# Where the per-run evidence goes, when a remediation run asks for it.
EVIDENCE_VARIABLE = "WASLA_RACE_EVIDENCE"


class _Paymob(PaymobProvider):
    """The real adapter over a fake socket, able to answer an inquiry.

    Intentions answer with the order our own reference derives (see
    `tests/paymob_orders.py`); an inquiry answers with whichever signed
    transaction the test queued for that reference. Everything that decides
    anything - verification, binding, settlement - is the production code.
    """

    def __init__(self) -> None:
        self.answers: dict[str, dict[str, Any]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/v1/intention" in str(request.url):
                return httpx.Response(
                    201,
                    json={
                        "id": f"pi_test_{uuid.uuid4().hex[:12]}",
                        "client_secret": f"egy_csk_test_{uuid.uuid4().hex[:12]}",
                        "intention_order_id": order_from_request(request),
                    },
                )
            return httpx.Response(404, json={})

        super().__init__(
            secret_key="egy_sk_test_race",
            public_key="egy_pk_test_race",
            hmac_secret=HMAC_SECRET,
            integration_ids=[CARD_INTEGRATION_ID],
            transport=httpx.MockTransport(handler),
        )

    @property
    def can_inquire(self) -> bool:
        return True

    async def inquire_charge(self, reference: str) -> ChargeInquiry:
        obj = self.answers[reference]
        body = json.dumps({"type": "TRANSACTION", "obj": obj}).encode()
        event = self.verify_callback(
            payload=body, signature=hmac_signature(obj, secret=HMAC_SECRET)
        )
        return ChargeInquiry(verdict=InquiryVerdict.ANSWERED, event=event)


def _transaction(payment_id: uuid.UUID, order: str, *, transaction: int) -> dict[str, Any]:
    return {
        "id": transaction,
        "pending": False,
        "amount_cents": int(PRICE * 100),
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": CARD_INTEGRATION_ID,
        "has_parent_transaction": False,
        "order": {"id": int(order), "merchant_order_id": str(payment_id)},
        "is_live": False,
        "created_at": "2026-09-26T10:00:00.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }


def _transaction_id() -> int:
    return 700_000_000 + uuid.uuid4().int % 200_000_000


# --------------------------------------------------------------- the world


@dataclass
class World:
    """One committed workspace with an invoice (or two) and payments to race."""

    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    staff_id: uuid.UUID
    plan_ids: list[uuid.UUID]
    product_ids: list[uuid.UUID] = field(default_factory=list)
    invoice_ids: list[uuid.UUID] = field(default_factory=list)
    payment_ids: list[uuid.UUID] = field(default_factory=list)
    orders: dict[uuid.UUID, str] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))


@pytest_asyncio.fixture
async def maker(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, each on its own connection (NullPool)."""
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _base(session: AsyncSession, *, status: SubscriptionStatus) -> World:
    """Tenant, owner, platform staff and a 99 EGP plan the workspace is on."""
    now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name="Settlement Race", slug=f"settle-race-{tag}")
    staff = User(
        email=f"settle-staff-{tag}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    plan = Plan(
        code=f"settle-pro-{tag}",
        name="Pro",
        price=PRICE,
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.AGENTS.value: 5, LimitKey.PERIOD_AI_TURNS.value: 100},
    )
    session.add_all([tenant, staff, plan])
    await session.flush()
    owner = await add_owner(session, tenant)
    version = await PlanCatalog(session).current_version(plan)
    assert version is not None
    session.add(
        Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            plan_price_id=(await price_terms(session, version.id)).get("plan_price_id"),
            status=status,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            billing_anchor_at=now,
            cancel_at_period_end=False,
        )
    )
    await session.flush()
    return World(
        tenant_id=tenant.id, owner_id=owner.id, staff_id=staff.id, plan_ids=[plan.id], now=now
    )


async def _renewal(session: AsyncSession, world: World) -> Invoice:
    subscription = await session.scalar(
        text("SELECT id FROM subscriptions WHERE tenant_id = :t"), {"t": world.tenant_id}
    )
    version = await PlanCatalog(session).current_version(
        await session.get_one(Plan, world.plan_ids[0])
    )
    assert version is not None
    invoice = Invoice(
        tenant_id=world.tenant_id,
        subscription_id=subscription,
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.RENEWAL,
        plan_code="pro",
        plan_version_id=version.id,
        **(await price_terms(session, version.id)),
        amount_due=PRICE,
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=world.now,
        period_end=world.now + timedelta(days=30),
        issued_at=world.now,
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    world.invoice_ids.append(invoice.id)
    return invoice


async def _pending(session: AsyncSession, world: World, invoice_id: uuid.UUID) -> uuid.UUID:
    """A hosted payment page on `invoice_id`, bound to its Paymob order."""
    payment = Payment(
        tenant_id=world.tenant_id,
        invoice_id=invoice_id,
        status=PaymentStatus.PENDING,
        amount=PRICE,
        currency="EGP",
        provider="paymob",
        refunded_amount=Decimal("0.00"),
        is_automatic=False,
    )
    session.add(payment)
    await session.flush()
    payment.provider_order_id = str(order_for(payment.id))
    world.payment_ids.append(payment.id)
    world.orders[payment.id] = payment.provider_order_id
    return payment.id


async def renewal_with_pages(maker: async_sessionmaker[AsyncSession], pages: int) -> World:
    """A past-due workspace, one open renewal, `pages` hosted pages on it."""
    async with maker() as session:
        world = await _base(session, status=SubscriptionStatus.PAST_DUE)
        invoice = await _renewal(session, world)
        for _ in range(pages):
            await _pending(session, world, invoice.id)
        await session.commit()
    return world


async def renewal_with_real_pages(maker: async_sessionmaker[AsyncSession]) -> World:
    """Two pages opened through the real checkout on one open renewal: two tabs."""
    async with maker() as session:
        world = await _base(session, status=SubscriptionStatus.PAST_DUE)
        invoice = await _renewal(session, world)
        await session.commit()
    for _ in range(2):
        async with maker() as session:
            owner = await session.get_one(User, world.owner_id)
            started = await CheckoutService(
                session, tenant_id=world.tenant_id, provider=_Paymob()
            ).start(invoice_id=invoice.id, actor=owner)
            payment = await session.get_one(Payment, started.payment_id)
            world.payment_ids.append(payment.id)
            world.orders[payment.id] = str(payment.provider_order_id)
            await session.commit()
    return world


async def offer_with_two_pages(maker: async_sessionmaker[AsyncSession]) -> World:
    """A custom plan offer accepted twice: two pages, two checkout invoices."""
    async with maker() as session:
        world = await _base(session, status=SubscriptionStatus.ACTIVE)
        staff = await session.get_one(User, world.staff_id)
        created = await PlanCatalogAdmin(session).create(
            PlanCreate(
                code=f"race-abc-{uuid.uuid4().hex[:6]}",
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
            now=world.now,
        )
        assert created.current_version is not None
        world.plan_ids.append(created.id)
        await PlatformCustomPlanOffers(session).offer(
            world.tenant_id,
            CustomPlanOfferCreate(
                plan_version_id=created.current_version.id,
                expires_at=None,
                reason="Enterprise deal.",
            ),
            actor=staff,
            now=world.now,
        )
        await session.commit()
    for _ in range(2):
        async with maker() as session:
            owner = await session.get_one(User, world.owner_id)
            offer_id = await session.scalar(
                text("SELECT id FROM custom_plan_offers WHERE tenant_id = :t"),
                {"t": world.tenant_id},
            )
            accepted = await CustomPlanOfferService(
                session,
                tenant_id=world.tenant_id,
                checkout=CheckoutService(session, tenant_id=world.tenant_id, provider=_Paymob()),
            ).accept(offer_id, actor=owner, idempotency_key=None, now=world.now)
            payment = await session.get_one(Payment, accepted.checkout.payment_id)
            world.invoice_ids.append(accepted.checkout.invoice_id)
            world.payment_ids.append(payment.id)
            world.orders[payment.id] = str(payment.provider_order_id)
            await session.commit()
    return world


async def topup_with_two_attempts(maker: async_sessionmaker[AsyncSession]) -> World:
    """One top-up purchase whose invoice carries two collecting attempts."""
    async with maker() as session:
        world = await _base(session, status=SubscriptionStatus.ACTIVE)
        item = TopupProduct(
            code=f"race-tu-{uuid.uuid4().hex[:8]}",
            name="AI turns +5",
            entitlement_key=TopupEntitlement.PERIOD_AI_TURNS,
            quantity=5,
            price=PRICE,
            currency="EGP",
            scope=TopupScope.GLOBAL,
            is_active=True,
            is_public=True,
            validity_policy=TopupValidity.CURRENT_PERIOD_END,
        )
        session.add(item)
        await session.flush()
        world.product_ids.append(item.id)
        owner = await session.get_one(User, world.owner_id)
        started = await TopupService(
            session,
            tenant_id=world.tenant_id,
            checkout=CheckoutService(session, tenant_id=world.tenant_id, provider=_Paymob()),
        ).start_checkout(item.id, actor=owner, idempotency_key=None, now=world.now)
        payment = await session.get_one(Payment, started.payment_id)
        world.invoice_ids.append(payment.invoice_id)
        world.payment_ids.append(payment.id)
        world.orders[payment.id] = str(payment.provider_order_id)
        await _pending(session, world, payment.invoice_id)
        await session.commit()
    return world


async def _erase(maker: async_sessionmaker[AsyncSession], world: World) -> None:
    async with maker() as session:
        await erase_ledger(session, [world.tenant_id])
        for statement in (
            "DELETE FROM custom_plan_offers WHERE tenant_id = :t",
            "DELETE FROM subscriptions WHERE tenant_id = :t",
            "DELETE FROM audit_logs WHERE tenant_id = :t",
        ):
            await session.execute(text(statement), {"t": world.tenant_id})
        await session.execute(delete(Plan).where(Plan.id.in_(world.plan_ids[1:])))
        await session.execute(delete(Tenant).where(Tenant.id == world.tenant_id))
        await session.execute(delete(Plan).where(Plan.id == world.plan_ids[0]))
        await session.execute(delete(TopupProduct).where(TopupProduct.id.in_(world.product_ids)))
        await session.execute(
            text("DELETE FROM audit_logs WHERE actor_id = ANY(:u)"),
            {"u": [world.owner_id, world.staff_id]},
        )
        await session.execute(delete(User).where(User.id.in_([world.owner_id, world.staff_id])))
        await session.commit()


# --------------------------------------------------------------- racers


Racer = Callable[[async_sessionmaker[AsyncSession], World], Awaitable[str]]


def callback(page: int, *, transaction: int | None = None) -> Racer:
    """Paymob reports a successful transaction on page `page`."""

    async def run(maker: async_sessionmaker[AsyncSession], world: World) -> str:
        payment_id = world.payment_ids[page]
        obj = _transaction(
            payment_id, world.orders[payment_id], transaction=transaction or _transaction_id()
        )
        provider = _Paymob()
        event = provider.verify_callback(
            payload=json.dumps({"type": "TRANSACTION", "obj": obj}).encode(),
            signature=hmac_signature(obj, secret=HMAC_SECRET),
        )
        async with maker() as session:
            outcome = await CheckoutService(
                session, tenant_id=world.tenant_id, provider=provider
            ).apply(event, now=world.now)
            await session.commit()
            return outcome

    return run


def reconciliation(page: int) -> Racer:
    """The hosted-payment sweep asks about page `page` and settles the answer."""

    async def run(maker: async_sessionmaker[AsyncSession], world: World) -> str:
        payment_id = world.payment_ids[page]
        provider = _Paymob()
        provider.answers[str(payment_id)] = _transaction(
            payment_id, world.orders[payment_id], transaction=_transaction_id()
        )
        async with maker() as session:
            verdict = await PaymentReconciler(session=session, provider=provider).reconcile_hosted(
                payment_id, tenant_id=world.tenant_id, now=world.now
            )
            await session.commit()
            return APPLIED if verdict == "settled" else f"reconciled:{verdict}"

    return run


def legacy_manual() -> Racer:
    """`POST /platform/invoices/{id}/payments`: an operator records a transfer."""

    async def run(maker: async_sessionmaker[AsyncSession], world: World) -> str:
        async with maker() as session:
            staff = await session.get_one(User, world.staff_id)
            await PlatformBillingService(session).record_payment(
                invoice_id=world.invoice_ids[0],
                amount=PRICE,
                provider="bank_transfer",
                reference=f"BT-{uuid.uuid4().hex[:6]}",
                actor=staff,
            )
            await session.commit()
            return APPLIED

    return run


def locked_manual(revision: dict[str, int]) -> Racer:
    """`POST /platform/billing/invoices/{id}/payments`, the operator route."""

    async def run(maker: async_sessionmaker[AsyncSession], world: World) -> str:
        async with maker() as session:
            staff = await session.get_one(User, world.staff_id)
            await PlatformBillingOperations(
                session, settings=Settings(_env_file=None, environment="test")
            ).record_manual_payment(
                world.invoice_ids[0],
                ManualPaymentCreate(
                    amount=PRICE,
                    currency="EGP",
                    method="bank_transfer",
                    reason="Transfer seen on the statement.",
                    expected_revision=revision["value"],
                ),
                actor=staff,
            )
            await session.commit()
            return APPLIED

    return run


async def _attempt(racer: Racer, maker: async_sessionmaker[AsyncSession], world: World) -> str:
    try:
        return await racer(maker, world)
    except WaslaError as error:
        # A refusal an operator is shown (409, 404): an answer, not a failure.
        return f"refused:{type(error).__name__}"
    except Exception as error:  # recorded, never hidden - asserted absent below
        return f"EXC:{type(error).__name__}:{sqlstate(error)}"


@contextmanager
def meet_before_locking(racers: int) -> Iterator[None]:
    """Hold each racer at its first settlement lock until all have arrived.

    Before any lock is taken, so a racer waiting here holds nothing and the
    barrier cannot itself deadlock. The timeout is a guard, never the design.
    """
    original = InvoiceSettlement.lock
    barrier = asyncio.Barrier(racers)
    arrived: set[int] = set()

    async def lock(self: InvoiceSettlement, **kwargs: Any) -> Any:
        task = id(asyncio.current_task())
        if task not in arrived:
            arrived.add(task)
            async with asyncio.timeout(15):
                await barrier.wait()
        return await original(self, **kwargs)

    InvoiceSettlement.lock = lock  # type: ignore[method-assign]
    try:
        yield
    finally:
        InvoiceSettlement.lock = original  # type: ignore[method-assign]


async def race(
    maker: async_sessionmaker[AsyncSession],
    world: World,
    racers: list[Racer],
    *,
    barrier: bool,
) -> list[str]:
    if barrier:
        with meet_before_locking(len(racers)):
            return list(await asyncio.gather(*(_attempt(r, maker, world) for r in racers)))
    return list(await asyncio.gather(*(_attempt(r, maker, world) for r in racers)))


# --------------------------------------------------------------- the books


async def books(maker: async_sessionmaker[AsyncSession], world: World) -> dict[str, Any]:
    """The workspace's money, read by plain SQL rather than repository code."""
    async with maker() as session:
        row = (
            await session.execute(
                text("""
                SELECT
                  (SELECT count(*) FROM invoices WHERE tenant_id = :t AND status = 'paid'),
                  (SELECT count(*) FROM invoices i WHERE i.tenant_id = :t
                     AND i.amount_paid <> coalesce((SELECT sum(p.amount - p.refunded_amount)
                         FROM payments p WHERE p.invoice_id = i.id
                          AND p.applied_at IS NOT NULL), 0)),
                  (SELECT count(*) FROM invoices WHERE tenant_id = :t
                     AND amount_paid > amount_due),
                  (SELECT coalesce(sum(amount_paid), 0) FROM invoices WHERE tenant_id = :t),
                  (SELECT count(*) FROM payments WHERE tenant_id = :t
                     AND applied_at IS NOT NULL),
                  (SELECT coalesce(sum(amount - refunded_amount), 0) FROM payments
                    WHERE tenant_id = :t AND status IN ('succeeded', 'refunded')),
                  (SELECT coalesce(sum(amount - refunded_amount), 0) FROM payments
                    WHERE tenant_id = :t AND status IN ('succeeded', 'refunded')
                      AND applied_at IS NULL),
                  (SELECT count(*) FROM payments p WHERE p.tenant_id = :t
                     AND p.status IN ('succeeded', 'refunded') AND p.applied_at IS NULL
                     AND NOT EXISTS (SELECT 1 FROM billing_incidents b
                                      WHERE b.payment_id = p.id)),
                  (SELECT count(*) FROM billing_incidents WHERE tenant_id = :t
                     AND kind IN ('duplicate_payment', 'topup_duplicate_payment')),
                  (SELECT count(*) FROM audit_logs WHERE tenant_id = :t
                     AND action = 'payment_recorded' AND metadata ? 'invoice_status'),
                  (SELECT count(*) FROM audit_logs WHERE tenant_id = :t
                     AND action = 'billing_custom_plan_offer_activated'),
                  (SELECT count(*) FROM custom_plan_offers WHERE tenant_id = :t
                     AND status = 'active'),
                  (SELECT count(*) FROM topup_purchases WHERE tenant_id = :t
                     AND status = 'granted'),
                  (SELECT status::text FROM subscriptions WHERE tenant_id = :t)
                """),
                {"t": world.tenant_id},
            )
        ).one()
    keys = (
        "paid_invoices",
        "unreconciled_invoices",
        "over_collected_invoices",
        "amount_paid",
        "applied_payments",
        "collected",
        "held",
        "unexplained_held",
        "duplicate_incidents",
        "payment_recorded",
        "offer_activations",
        "active_offers",
        "granted_topups",
        "subscription",
    )
    return dict(zip(keys, row, strict=True))


def _record(
    scenario: str, run: int, barrier: bool, outcomes: list[str], ledger: dict[str, Any]
) -> None:
    path = os.environ.get(EVIDENCE_VARIABLE)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "scenario": scenario,
                    "run": run,
                    "barrier": barrier,
                    "outcomes": sorted(outcomes),
                    **{key: str(value) for key, value in ledger.items()},
                }
            )
            + "\n"
        )


def assert_one_settlement(outcomes: list[str], ledger: dict[str, Any], *, held: int) -> None:
    """The invariants every run must end with. See the module docstring."""
    failures = [outcome for outcome in outcomes if outcome.startswith("EXC:")]
    assert not failures, f"a racer failed rather than being refused: {outcomes}"
    assert outcomes.count(APPLIED) == 1, outcomes
    assert ledger["paid_invoices"] == 1, ledger
    assert ledger["amount_paid"] == PRICE, ledger
    assert ledger["applied_payments"] == 1, ledger
    assert ledger["unreconciled_invoices"] == 0, ledger
    assert ledger["over_collected_invoices"] == 0, ledger
    assert ledger["unexplained_held"] == 0, ledger
    assert ledger["held"] == PRICE * held, ledger
    # Accepted money + held money = everything a provider said it collected.
    assert ledger["amount_paid"] + ledger["held"] == ledger["collected"], ledger
    assert ledger["payment_recorded"] == 1, ledger


# --------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    name: str
    build: Callable[[async_sessionmaker[AsyncSession]], Awaitable[World]]
    racers: Callable[[World], list[Racer]]
    # Collected payments left held (a second page paid) - zero when the loser
    # never collected anything (an operator refused with 409) or collected on
    # the same payment row (a second transaction becomes an incident only).
    held: Callable[[list[str]], int]


def _held_callbacks(outcomes: list[str]) -> int:
    return outcomes.count(REFUSED)


SCENARIOS = [
    Scenario(
        "two_hosted_pages_one_renewal",
        lambda m: renewal_with_pages(m, 2),
        lambda w: [callback(0), callback(1)],
        _held_callbacks,
    ),
    Scenario(
        "two_pages_opened_by_the_real_checkout",
        renewal_with_real_pages,
        lambda w: [callback(0), callback(1)],
        _held_callbacks,
    ),
    Scenario(
        "legacy_manual_twice",
        lambda m: renewal_with_pages(m, 0),
        lambda w: [legacy_manual(), legacy_manual()],
        lambda outcomes: 0,
    ),
    Scenario(
        "callback_vs_legacy_manual",
        lambda m: renewal_with_pages(m, 1),
        lambda w: [callback(0), legacy_manual()],
        _held_callbacks,
    ),
    Scenario(
        "callback_vs_operator_manual",
        lambda m: renewal_with_pages(m, 1),
        lambda w: [callback(0), locked_manual({"value": 1})],
        _held_callbacks,
    ),
    Scenario(
        "callback_vs_reconciliation",
        lambda m: renewal_with_pages(m, 2),
        lambda w: [callback(0), reconciliation(1)],
        lambda outcomes: outcomes.count(REFUSED) + outcomes.count("reconciled:still_pending"),
    ),
    Scenario(
        "two_transactions_on_one_payment",
        lambda m: renewal_with_pages(m, 1),
        lambda w: [callback(0), callback(0)],
        lambda outcomes: 0,
    ),
    Scenario(
        "two_pages_of_one_custom_offer",
        offer_with_two_pages,
        lambda w: [callback(0), callback(1)],
        _held_callbacks,
    ),
    Scenario(
        "two_attempts_on_one_topup",
        topup_with_two_attempts,
        lambda w: [callback(0), callback(1)],
        _held_callbacks,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_two_settlements_of_one_invoice_apply_exactly_once(
    maker: async_sessionmaker[AsyncSession], scenario: Scenario
) -> None:
    for run in range(RUNS):
        barrier = run % 2 == 0
        world = await scenario.build(maker)
        try:
            outcomes = await race(maker, world, scenario.racers(world), barrier=barrier)
            ledger = await books(maker, world)
            _record(scenario.name, run, barrier, outcomes, ledger)
            assert_one_settlement(outcomes, ledger, held=scenario.held(outcomes))
            if scenario.name == "two_transactions_on_one_payment":
                # A second success on a paid order is an incident, not a row.
                assert sorted(outcomes) == [APPLIED, REFUSED]
                assert ledger["duplicate_incidents"] == 1, ledger
            if scenario.name == "two_pages_of_one_custom_offer":
                assert ledger["offer_activations"] == 1, ledger
                assert ledger["active_offers"] == 1, ledger
            if scenario.name == "two_attempts_on_one_topup":
                assert ledger["granted_topups"] == 1, ledger
            if "renewal" in scenario.name or "manual" in scenario.name:
                # The past-due workspace was restored, once.
                assert ledger["subscription"] == "active", ledger
        finally:
            await _erase(maker, world)


@contextmanager
def without_the_settlement_lock() -> Iterator[None]:
    """Settlement as it was before DB-001: rows read, nothing locked."""
    original = InvoiceSettlement.lock

    async def unlocked(self: InvoiceSettlement, **kwargs: Any) -> Any:
        from app.services.settlement_service import SettlementRows

        session = self._session
        payment_id = kwargs.get("payment_id")
        return SettlementRows(
            payment=await session.get(Payment, payment_id) if payment_id else None,
            invoice=await session.get_one(Invoice, kwargs["invoice_id"]),
            subscription=await self._subscriptions.get(),
        )

    InvoiceSettlement.lock = unlocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        InvoiceSettlement.lock = original  # type: ignore[method-assign]


async def test_the_database_refuses_a_settlement_that_skipped_the_lock(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """With the application's lock removed, the database still keeps the books.

    The audit's reproduction exactly - two pages, both paid at once, nothing
    locked - and the backstop from migration 0074 is what stands: one
    settlement commits, and the other's commit is refused (SQLSTATE 23000)
    and rolls back whole, callback claim included, for Paymob to retry into
    an ordinary refusal. No run may end with money counted twice.
    """
    refused = 0
    for run in range(RUNS):
        world = await renewal_with_pages(maker, 2)
        try:
            with without_the_settlement_lock(), meet_before_locking(2):
                outcomes = await asyncio.gather(
                    *(_attempt(r, maker, world) for r in (callback(0), callback(1)))
                )
            ledger = await books(maker, world)
            _record("backstop_without_lock", run, True, list(outcomes), ledger)
            assert ledger["unreconciled_invoices"] == 0, ledger
            assert ledger["over_collected_invoices"] == 0, ledger
            assert ledger["unexplained_held"] == 0, ledger
            assert ledger["applied_payments"] <= 1, ledger
            assert ledger["amount_paid"] + ledger["held"] == ledger["collected"], ledger
            assert outcomes.count(APPLIED) >= 1, outcomes
            refused += sum(1 for o in outcomes if o.startswith("EXC:") and o.endswith(":23000"))
            for outcome in outcomes:
                assert outcome in (APPLIED, REFUSED) or outcome.endswith(":23000"), outcomes
        finally:
            await _erase(maker, world)
    # The database, not luck, decided: at least one run lost the race at commit.
    assert refused >= 1
