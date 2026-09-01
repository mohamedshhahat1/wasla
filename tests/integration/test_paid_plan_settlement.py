"""The join between the money path and the plan a workspace is on.

Both halves were already strict and neither knew about the other (ADR-059).

`CheckoutService` would not settle an invoice without an HMAC over the
provider's own payload, a reference this system generated, a matching amount and
currency and a legal state transition. `SubscriptionService` would move a
workspace onto any public plan the moment an owner asked, with no reference to
an invoice at all. So the payment pipeline was optional decoration around a
self-service upgrade that cost nothing, and every existing test passed: the
billing tests exercised the money, the entitlement tests exercised the limits,
and nothing exercised the sentence connecting them.

That sentence is now: **a plan with a price is granted only by settlement.** The
tests below are written against that property from both ends - what may not be
asked for, and what a verified payment actually does - and they use the real
services and the real signature, because a stub on either side would be testing
the fixture rather than the invariant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_entitlement_service,
)
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.core.exceptions import PaymentRequiredError
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import TenantRole
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.paymob import hmac_signature
from app.main import create_app
from app.services.entitlement_service import EntitlementService
from app.services.subscription_service import SubscriptionService
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

WEBHOOK = "/api/v1/webhooks/paymob"
HMAC_SECRET = "a-test-hmac-secret"
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


class _Infra:
    def __init__(self) -> None:
        self.commands = self

    @property
    def client(self):
        return self.commands

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def paying_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        billing_provider="paymob",
        paymob_secret_key="sk_test_notreal",
        paymob_public_key="pk_test_notreal",
        paymob_hmac_secret=HMAC_SECRET,
        paymob_integration_ids=[4097558],
        app_public_url="https://app.example.com",
    )


@pytest.fixture
def app(paying_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(paying_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session(request: Request) -> AsyncIterator[AsyncSession]:
        setattr(request.state, SESSION_STATE_ATTRIBUTE, db_session)
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


# ------------------------------------------------------------------ fixtures


async def _tenant(session: AsyncSession, slug: str | None = None) -> Tenant:
    tenant = Tenant(name="Acme", slug=slug or f"acme-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(
    session: AsyncSession,
    *,
    code: str | None = None,
    price: str,
    agents: int,
) -> Plan:
    plan = Plan(
        code=code or f"plan-{uuid.uuid4().hex[:6]}",
        name="Pro",
        price=Decimal(price),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.AGENTS.value: agents},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscribed(session: AsyncSession, *, tenant: Tenant, plan: Plan) -> Subscription:
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=NOW,
        current_period_end=NOW + timedelta(days=30),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def _checkout_rows(
    session: AsyncSession,
    *,
    tenant: Tenant,
    plan: Plan,
    subscription: Subscription | None,
) -> tuple[Invoice, Payment]:
    """What `CheckoutService.start` writes before the provider is ever called."""
    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=subscription.id if subscription else None,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=plan.price,
        amount_paid=Decimal("0.00"),
        currency=plan.currency,
        period_start=NOW,
        period_end=NOW + timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=plan.price,
        currency=plan.currency,
        provider="paymob",
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


def _transaction(*, reference: str | None, amount_cents: int, **overrides) -> dict:
    transaction: dict = {
        "id": 900000000 + uuid.uuid4().int % 10_000_000,
        "pending": False,
        "amount_cents": amount_cents,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": 4097558,
        "has_parent_transaction": False,
        "order": {"id": 217503754, "merchant_order_id": reference},
        "created_at": "2026-08-27T11:33:44.592345",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    transaction.update(overrides)
    return transaction


async def _deliver(http: AsyncClient, transaction: dict):
    """A callback exactly as Paymob sends one, signed with the real scheme."""
    signature = hmac_signature(transaction, secret=HMAC_SECRET)
    return await http.post(
        WEBHOOK,
        params={"hmac": signature},
        json={"type": "TRANSACTION", "obj": transaction},
    )


async def _agent_limit(session: AsyncSession, tenant: Tenant) -> int | None:
    entitlement = await EntitlementService(session, tenant_id=tenant.id).check(
        LimitKey.AGENTS, additional=0
    )
    return entitlement.limit


# ------------------------------------------- A. the free upgrade, closed


async def test_a_priced_plan_cannot_be_chosen_without_paying(db_session: AsyncSession) -> None:
    """The exploit: `change_plan` used to grant the limits outright."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code=free.code, now=NOW)

    with pytest.raises(PaymentRequiredError):
        await service.change_plan(plan_code=paid.code, now=NOW)

    assert await _agent_limit(db_session, tenant) == 1


async def test_a_priced_plan_cannot_be_started_without_paying(db_session: AsyncSession) -> None:
    """The same hole by the other door, for a workspace with no subscription."""
    tenant = await _tenant(db_session)
    paid = await _plan(db_session, price="99.00", agents=25)
    service = SubscriptionService(db_session, tenant_id=tenant.id)

    with pytest.raises(PaymentRequiredError):
        await service.start(plan_code=paid.code, now=NOW)

    assert await service.get() is None


async def test_the_refusal_answers_402_over_http(
    app: FastAPI,
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """402 with its own code: reach for a card, not for an administrator.

    The workspace is overridden rather than signed into, because what this
    asserts is the status and the error code a client renders from - the
    authorization that precedes it is covered by `test_billing_authorization`.
    """
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    await SubscriptionService(db_session, tenant_id=tenant.id).start(plan_code=free.code, now=NOW)
    owner = User(email=f"owner-{uuid.uuid4().hex[:6]}@example.com", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    membership = Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER)
    db_session.add(membership)
    await db_session.flush()

    workspace = ActiveWorkspace(user=owner, membership=membership, tenant=tenant)
    app.dependency_overrides[get_active_workspace] = lambda: workspace

    response = await http.post(
        "/api/v1/billing/subscription/plan",
        json={"plan_code": paid.code},
    )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_required"
    assert await _agent_limit(db_session, tenant) == 1


async def test_a_free_plan_is_still_self_service(db_session: AsyncSession) -> None:
    """The fix must not take the free tier away.

    Downgrading, and every deployment whose catalogue costs nothing, still
    works without a payment existing anywhere.
    """
    tenant = await _tenant(db_session)
    first = await _plan(db_session, price="0.00", agents=1)
    second = await _plan(db_session, price="0.00", agents=3)
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code=first.code, now=NOW)

    changed = await service.change_plan(plan_code=second.code, now=NOW)

    assert changed.plan_id == second.id
    assert await _agent_limit(db_session, tenant) == 3


# ------------------------------------------- B. what a settled payment does


async def test_a_verified_callback_is_what_grants_the_paid_plan(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The whole invariant, from an unpaid invoice to applied entitlements."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    invoice, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )

    # Before: the invoice exists, nothing has been paid, nothing is granted.
    assert await _agent_limit(db_session, tenant) == 1

    delivered = await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    assert delivered.status_code == 200
    await db_session.refresh(subscription)
    await db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
    assert subscription.plan_id == paid.id
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert await _agent_limit(db_session, tenant) == 25


async def test_a_declined_payment_grants_nothing(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """C. The dangerous failure is not a crash - it is a plan granted anyway."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )

    declined = await _deliver(
        http,
        _transaction(
            reference=str(payment.id),
            amount_cents=9900,
            success=False,
            error_occured=True,
            data={"message": "Insufficient funds"},
        ),
    )

    assert declined.status_code == 200
    await db_session.refresh(subscription)
    assert subscription.plan_id == free.id
    assert await _agent_limit(db_session, tenant) == 1


async def test_an_unsigned_callback_grants_nothing(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """C, again: a forged payment is refused before it can reach a subscription."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )

    forged = await http.post(
        WEBHOOK,
        params={"hmac": "0" * 128},
        json={
            "type": "TRANSACTION",
            "obj": _transaction(reference=str(payment.id), amount_cents=9900),
        },
    )

    assert forged.status_code == 403
    await db_session.refresh(subscription)
    assert subscription.plan_id == free.id


async def test_a_callback_reporting_the_wrong_amount_grants_nothing(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Signed, and still refused: the figures have to match what we asked for."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )

    delivered = await _deliver(http, _transaction(reference=str(payment.id), amount_cents=100))

    assert delivered.status_code == 200
    await db_session.refresh(subscription)
    assert subscription.plan_id == free.id


async def test_a_replayed_callback_moves_the_plan_once(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """D. Idempotency now has a plan transition riding on it, not only money."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    invoice, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )
    transaction = _transaction(reference=str(payment.id), amount_cents=9900)

    first = await _deliver(http, transaction)
    await db_session.refresh(subscription)
    period_after_first = subscription.current_period_start
    second = await _deliver(http, transaction)

    assert (first.status_code, second.status_code) == (200, 200)
    await db_session.refresh(subscription)
    await db_session.refresh(invoice)
    assert subscription.plan_id == paid.id
    # The second delivery changed nothing: a fresh transition would have
    # restarted the period.
    assert subscription.current_period_start == period_after_first
    assert invoice.amount_paid == Decimal("99.00")


async def test_a_payment_cannot_move_another_workspaces_plan(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """E. The callback names a payment, and the payment names its own tenant."""
    paying = await _tenant(db_session, slug=f"paying-{uuid.uuid4().hex[:6]}")
    bystander = await _tenant(db_session, slug=f"bystander-{uuid.uuid4().hex[:6]}")
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    paying_subscription = await _subscribed(db_session, tenant=paying, plan=free)
    other_subscription = await _subscribed(db_session, tenant=bystander, plan=free)
    _, payment = await _checkout_rows(
        db_session, tenant=paying, plan=paid, subscription=paying_subscription
    )

    await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    await db_session.refresh(paying_subscription)
    await db_session.refresh(other_subscription)
    assert paying_subscription.plan_id == paid.id
    assert other_subscription.plan_id == free.id
    assert await _agent_limit(db_session, bystander) == 1


# --------------------------------------- what settlement must still leave alone


async def test_paying_a_renewal_does_not_restart_the_period(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An invoice for the plan already held is a renewal, not a transition.

    The sweep owns period arithmetic. Settlement moving the plan here would
    restart the month every time somebody paid a bill.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=plan)
    started = subscription.current_period_start
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=plan, subscription=subscription
    )

    await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    await db_session.refresh(subscription)
    assert subscription.plan_id == plan.id
    assert subscription.current_period_start == started


async def test_paying_an_old_invoice_does_not_revive_a_cancelled_subscription(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Paying is not a request to resubscribe - the rule `_settle` already kept."""
    tenant = await _tenant(db_session)
    free = await _plan(db_session, price="0.00", agents=1)
    paid = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=free)
    subscription.status = SubscriptionStatus.CANCELLED
    await db_session.flush()
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=paid, subscription=subscription
    )

    await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.plan_id == free.id


async def test_a_settled_payment_still_clears_past_due(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The behaviour that existed before this change, unchanged by it."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, price="99.00", agents=25)
    subscription = await _subscribed(db_session, tenant=tenant, plan=plan)
    subscription.status = SubscriptionStatus.PAST_DUE
    await db_session.flush()
    _, payment = await _checkout_rows(
        db_session, tenant=tenant, plan=plan, subscription=subscription
    )

    await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_a_paid_invoice_with_no_subscription_settles_and_grants_nothing(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The misconfigured deployment, recorded rather than guessed at.

    Every workspace gets a subscription at registration, so this is the state
    where `DEFAULT_PLAN_CODE` names no plan - and where limits are already
    unenforced. The money is still recorded; inventing trial and period rules
    inside a settlement path is the parallel state machine this fix avoids.
    """
    tenant = await _tenant(db_session)
    paid = await _plan(db_session, price="99.00", agents=25)
    invoice, payment = await _checkout_rows(db_session, tenant=tenant, plan=paid, subscription=None)

    delivered = await _deliver(http, _transaction(reference=str(payment.id), amount_cents=9900))

    assert delivered.status_code == 200
    await db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
    assert (
        await db_session.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id))
    ) is None
