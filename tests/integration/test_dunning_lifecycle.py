"""What happens to a workspace that stops paying, all the way to the end.

The defect this file exists for is the second half of the one ADR-059 closed.
That change made a paid plan unobtainable without a settled payment; this one is
about *retention*. `_chase_unpaid` moved a workspace to `PAST_DUE` and nothing
moved it anywhere afterwards - and `PAST_DUE` is a serving status, so a
workspace that simply stopped paying kept its paid plan for ever. The purchase
was protected and the retention was not, which is the same product given away by
a slower route (ADR-061).

**The assertions are about entitlements, not about a status column.** A test
that only checked `subscription.status is SUSPENDED` would pass against an
implementation that changed the label and went on serving the plan, which is
precisely the bug. So every lifecycle test below asks `EntitlementService` what
the workspace may actually do.

**Every moment is explicit.** `NOW` is a fixed datetime and every invoice states
its own `issued_at`; nothing reads the wall clock. Phase 0 lost a CI run to a
test whose rows landed on `now()` and fell outside a hardcoded window when the
month rolled over, and threshold tests are the easiest place in the codebase to
reintroduce that.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.email import OutboundEmail
from app.db.models.enums import TenantRole
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.paymob import hmac_signature
from app.main import create_app
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.services.entitlement_service import EntitlementService
from app.workers.billing_worker import BillingWorker
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

WEBHOOK = "/api/v1/webhooks/paymob"
HMAC_SECRET = "a-test-hmac-secret"

# Every moment in this file is derived from this one. Nothing calls `now()`.
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

# The thresholds under test, named here so a boundary test reads as a boundary
# rather than as arithmetic.
PAST_DUE_DAYS = 7
SUSPEND_DAYS = 30

FREE_AGENTS = 1
PAID_AGENTS = 25


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


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


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
        # Generated rather than written down, following the sibling suite: a
        # literal long enough to satisfy the setting also looks like a leaked
        # credential to a secret scanner.
        "jwt_secret": secrets.token_urlsafe(32),
        "billing_past_due_days": PAST_DUE_DAYS,
        "billing_suspend_after_days": SUSPEND_DAYS,
        # Email on, so the notices land somewhere a test can count them.
        "email_enabled": True,
        "email_provider": "fake",
        "email_from": "no-reply@wasla.test",
        "app_public_url": "https://app.wasla.test",
        "billing_provider": "paymob",
        "paymob_secret_key": "sk_test_notreal",
        "paymob_public_key": "pk_test_notreal",
        "paymob_hmac_secret": HMAC_SECRET,
        "paymob_integration_ids": [4097558],
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def dunning_settings() -> Settings:
    return _settings()


@pytest.fixture
def app(dunning_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    """The real application, for the callback half of the lifecycle."""
    application = create_app(dunning_settings)
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


async def _tenant(session: AsyncSession, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _owner(session: AsyncSession, tenant: Tenant) -> User:
    """Somebody for a notice to be addressed to.

    `EmailOutbox.enqueue_for_tenant_owners` writes one row per *active owner*,
    so a workspace with no membership produces no mail however correct the
    transition is. The tests that count notices need a real recipient.
    """
    user = User(
        email=f"owner-{tenant.slug}@example.com",
        hashed_password="not-a-real-hash",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER))
    await session.flush()
    return user


async def _plan(session: AsyncSession, *, code: str, price: str, agents: int) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal(price),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        trial_days=0,
        limits={LimitKey.AGENTS.value: agents},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _paid_plan(session: AsyncSession) -> Plan:
    return await _plan(session, code="pro", price="99.00", agents=PAID_AGENTS)


async def _free_plan(session: AsyncSession) -> Plan:
    """The fallback `DEFAULT_PLAN_CODE` names, so degradation has a floor."""
    return await _plan(session, code="starter", price="0.00", agents=FREE_AGENTS)


async def _subscription(
    session: AsyncSession,
    tenant: Tenant,
    plan: Plan,
    *,
    status: SubscriptionStatus,
) -> Subscription:
    """Mid-period, so the roll-over half of the sweep leaves it alone."""
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=status,
        current_period_start=NOW - timedelta(days=10),
        current_period_end=NOW + timedelta(days=20),
    )
    await session.flush()
    return subscription


async def _unpaid_invoice(
    session: AsyncSession,
    tenant: Tenant,
    subscription: Subscription,
    *,
    issued_days_ago: float,
) -> Invoice:
    """A renewal invoice, issued a stated number of days before `NOW`.

    `issued_at` is what both thresholds read, and it is set explicitly here for
    the reason the module docstring gives: a row left to the column default
    would land on the wall clock and the test would mean something different
    tomorrow.
    """
    invoice = InvoiceRepository(session, tenant_id=tenant.id).create(
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        plan_code="pro",
        amount_due=Decimal("99.00"),
        currency="USD",
        period_start=NOW - timedelta(days=40),
        period_end=NOW - timedelta(days=10),
        lines=[],
    )
    await session.flush()
    invoice.issued_at = NOW - timedelta(days=issued_days_ago)
    await session.flush()
    return invoice


def _worker(session: AsyncSession, **overrides: object) -> BillingWorker:
    return BillingWorker(
        database=SessionHandle(session),  # type: ignore[arg-type]
        settings=_settings(**overrides),
    )


async def _agent_limit(session: AsyncSession, tenant: Tenant) -> int | None:
    """What the workspace may actually do, which is the real invariant."""
    entitlement = await EntitlementService(
        session,
        tenant_id=tenant.id,
        default_plan_code="starter",
    ).check(LimitKey.AGENTS, additional=0)
    return entitlement.limit


async def _audits(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.execute(select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars().all())


async def _notices(session: AsyncSession, template: str) -> list[OutboundEmail]:
    rows = await session.execute(select(OutboundEmail).where(OutboundEmail.template == template))
    return list(rows.scalars().all())


def _transaction(*, reference: str, amount_cents: int = 9900, **overrides: object) -> dict:
    transaction: dict = {
        "id": 700000000 + int(reference.replace("-", "")[:6], 16) % 1_000_000,
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
        "currency": "USD",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    transaction.update(overrides)
    return transaction


async def _pay(http: AsyncClient, payment: Payment):
    """A callback exactly as Paymob sends one, signed with the real scheme."""
    transaction = _transaction(reference=str(payment.id))
    signature = hmac_signature(transaction, secret=HMAC_SECRET)
    return await http.post(
        WEBHOOK,
        params={"hmac": signature},
        json={"type": "TRANSACTION", "obj": transaction},
    )


async def _pending_payment(
    session: AsyncSession,
    tenant: Tenant,
    invoice: Invoice,
) -> Payment:
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=Decimal("99.00"),
        currency="USD",
        provider="paymob",
    )
    session.add(payment)
    await session.flush()
    return payment


# ---------------------------------------------------- 1. soft: still serving


async def test_past_the_soft_threshold_the_workspace_is_behind_but_still_served(
    db_session: AsyncSession,
) -> None:
    """A failed card is a conversation, not a disconnection.

    The grace period is the product's promise that one late payment does not
    cut somebody off mid-sentence with their own customers, so this asserts the
    plan is *still* resolved - the opposite of the test below it.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.ACTIVE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=PAST_DUE_DAYS + 1)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert subscription.is_serving is True
    assert await _agent_limit(db_session, tenant) == PAID_AGENTS


async def test_inside_the_soft_threshold_nothing_moves(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.ACTIVE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=PAST_DUE_DAYS - 1)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert await _agent_limit(db_session, tenant) == PAID_AGENTS


# ------------------------------------------- 2. hard: entitlements disappear


async def test_past_the_hard_threshold_the_paid_plan_stops_applying(
    db_session: AsyncSession,
) -> None:
    """The defect, closed, asserted where it actually mattered.

    The status is checked, but the assertion that would have failed against the
    old code is the entitlement one: `PAST_DUE` served for ever, so the paid
    limits stayed resolved no matter how long the bill went unpaid.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 1)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.SUSPENDED
    assert subscription.is_serving is False
    # Degraded to the configured default plan, not to nothing: the workspace
    # keeps a usable free tier rather than being locked out, and the fallback
    # is `EntitlementService`'s existing behaviour rather than a second copy of
    # plan resolution inside the worker.
    assert await _agent_limit(db_session, tenant) == FREE_AGENTS


async def test_a_workspace_never_chased_is_told_before_it_is_cut_off(
    db_session: AsyncSession,
) -> None:
    """Both transitions in one sweep, in order, when the worker was down.

    An invoice already past the hard threshold the first time this loop sees it
    must still produce the past-due state and its notice, rather than a
    workspace being suspended having never been told anything.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.ACTIVE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 5)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.SUSPENDED
    assert len(await _audits(db_session, AuditAction.SUBSCRIPTION_PAST_DUE)) == 1
    assert len(await _audits(db_session, AuditAction.SUBSCRIPTION_SUSPENDED)) == 1


async def test_a_suspended_workspace_is_not_invoiced_again(
    db_session: AsyncSession,
) -> None:
    """Billing somebody you have cut off is worse than not billing them.

    `PlatformSubscriptionRepository.due` selects only the serving statuses, so
    a suspended subscription opens no new period and raises no further invoice.
    Asserted rather than assumed, because that exclusion is an allow-list a
    future edit could widen without noticing.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(
        db_session, tenant, paid, status=SubscriptionStatus.SUSPENDED
    )
    subscription.current_period_end = NOW - timedelta(hours=1)
    await db_session.flush()
    before = len(
        (await db_session.execute(select(Invoice).where(Invoice.tenant_id == tenant.id)))
        .scalars()
        .all()
    )

    await _worker(db_session).run_once(now=NOW)

    after = (
        (await db_session.execute(select(Invoice).where(Invoice.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert len(after) == before
    assert subscription.status is SubscriptionStatus.SUSPENDED


# --------------------------------------------------- 3. payment recovers it


async def test_paying_during_the_grace_restores_service(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`PAST_DUE` → `ACTIVE`, by the same signed callback as any other payment."""
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    invoice = await _unpaid_invoice(
        db_session, tenant, subscription, issued_days_ago=PAST_DUE_DAYS + 1
    )
    payment = await _pending_payment(db_session, tenant, invoice)

    delivered = await _pay(http, payment)

    assert delivered.status_code == 200
    await db_session.refresh(subscription)
    await db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert await _agent_limit(db_session, tenant) == PAID_AGENTS


async def test_paying_after_suspension_restores_service(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`SUSPENDED` → `ACTIVE`, and this is the recovery a new status buys.

    Reusing `CANCELLED` for non-payment would have made this impossible to
    express: settlement deliberately does not revive a subscription somebody
    chose to end, so a customer paying their overdue bill would have settled
    the invoice and stayed cut off.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(
        db_session, tenant, paid, status=SubscriptionStatus.SUSPENDED
    )
    invoice = await _unpaid_invoice(
        db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 2
    )
    payment = await _pending_payment(db_session, tenant, invoice)
    assert await _agent_limit(db_session, tenant) == FREE_AGENTS

    delivered = await _pay(http, payment)

    assert delivered.status_code == 200
    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.is_serving is True
    assert await _agent_limit(db_session, tenant) == PAID_AGENTS


# ------------------------------------------------ 4. no accidental revival


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED],
)
async def test_an_old_callback_does_not_revive_an_ended_subscription(
    http: AsyncClient,
    db_session: AsyncSession,
    status: SubscriptionStatus,
) -> None:
    """A decision somebody made is not undone by money arriving afterwards.

    The recovery above is a closed set of two statuses for exactly this reason.
    The payment is still recorded and the invoice still settles - the ledger
    must stay honest - but the subscription does not come back.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=status)
    invoice = await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=40)
    payment = await _pending_payment(db_session, tenant, invoice)

    delivered = await _pay(http, payment)

    assert delivered.status_code == 200
    await db_session.refresh(subscription)
    await db_session.refresh(invoice)
    await db_session.refresh(payment)
    # The money is recorded truthfully...
    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID
    # ...and the subscription stays where the customer left it.
    assert subscription.status is status
    assert subscription.is_serving is False


async def test_the_worker_does_not_suspend_a_cancelled_subscription(
    db_session: AsyncSession,
) -> None:
    """Only a `PAST_DUE` row is suspended, so an ended one is left as it is."""
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(
        db_session, tenant, paid, status=SubscriptionStatus.CANCELLED
    )
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 10)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert await _audits(db_session, AuditAction.SUBSCRIPTION_SUSPENDED) == []


# ------------------------------------------------------- 5. idempotency


async def test_sweeping_twice_suspends_once(db_session: AsyncSession) -> None:
    """The status is the claim, so only one pass can find it in `PAST_DUE`.

    A worker that runs every ten minutes against a workspace that stays
    suspended must not write an audit row or an email every cycle.
    """
    tenant = await _tenant(db_session)
    await _owner(db_session, tenant)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 1)

    worker = _worker(db_session)
    first = await worker.run_once(now=NOW)
    second = await worker.run_once(now=NOW + timedelta(minutes=10))

    assert first >= 1
    assert second == 0
    assert subscription.status is SubscriptionStatus.SUSPENDED
    assert len(await _audits(db_session, AuditAction.SUBSCRIPTION_SUSPENDED)) == 1
    assert len(await _notices(db_session, "subscription_suspended")) == 1


async def test_the_suspension_notice_is_keyed_to_the_invoice(
    db_session: AsyncSession,
) -> None:
    """Idempotent by key as well as by status, which is belt and braces.

    Even if a future edit let the transition run twice, the outbox key is the
    invoice - so the workspace is told once about this bill rather than once
    per sweep for ever.
    """
    tenant = await _tenant(db_session)
    await _owner(db_session, tenant)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    invoice = await _unpaid_invoice(
        db_session, tenant, subscription, issued_days_ago=SUSPEND_DAYS + 1
    )

    await _worker(db_session).run_once(now=NOW)
    # Put it back and sweep again: the key, not the status, is what refuses.
    subscription.status = SubscriptionStatus.PAST_DUE
    await db_session.flush()
    await _worker(db_session).run_once(now=NOW + timedelta(minutes=10))

    notices = await _notices(db_session, "subscription_suspended")
    assert len(notices) == 1
    assert str(invoice.id) in notices[0].idempotency_key


# ------------------------------------------------------- 6. tenant isolation


async def test_one_workspaces_unpaid_invoice_cannot_suspend_another(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, "debtor")
    bystander = await _tenant(db_session, "bystander")
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    behind = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    other = await _subscription(db_session, bystander, paid, status=SubscriptionStatus.ACTIVE)
    await _unpaid_invoice(db_session, tenant, behind, issued_days_ago=SUSPEND_DAYS + 1)

    await _worker(db_session).run_once(now=NOW)

    assert behind.status is SubscriptionStatus.SUSPENDED
    assert other.status is SubscriptionStatus.ACTIVE
    assert await _agent_limit(db_session, bystander) == PAID_AGENTS
    suspended = await _audits(db_session, AuditAction.SUBSCRIPTION_SUSPENDED)
    assert [row.tenant_id for row in suspended] == [tenant.id]


# ------------------------------------------------------------ 7. boundaries


@pytest.mark.parametrize(
    ("issued_days_ago", "expected"),
    [
        # Just inside: the threshold is strict, so a workspace one hour short
        # of it keeps its plan.
        (SUSPEND_DAYS - 0.5, SubscriptionStatus.PAST_DUE),
        # Exactly at it. `overdue` compares `issued_at < now - grace`, so an
        # invoice issued precisely `SUSPEND_DAYS` ago is not yet past it.
        (SUSPEND_DAYS, SubscriptionStatus.PAST_DUE),
        # Just past it.
        (SUSPEND_DAYS + 0.5, SubscriptionStatus.SUSPENDED),
    ],
)
async def test_the_hard_threshold_is_exact(
    db_session: AsyncSession,
    issued_days_ago: float,
    expected: SubscriptionStatus,
) -> None:
    """Before, at and after, against a fixed clock.

    The boundary is asserted rather than assumed because "unpaid for 30 days"
    is the sentence a customer will quote back, and an off-by-one here suspends
    somebody a day early.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=issued_days_ago)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is expected


async def test_the_thresholds_are_configuration(db_session: AsyncSession) -> None:
    """A deployment changes the numbers; nothing changes the code.

    Driven at a shorter grace than the default so the test also proves the
    worker reads settings rather than its module constants.
    """
    tenant = await _tenant(db_session)
    await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    subscription = await _subscription(db_session, tenant, paid, status=SubscriptionStatus.PAST_DUE)
    await _unpaid_invoice(db_session, tenant, subscription, issued_days_ago=4)

    await _worker(db_session, billing_past_due_days=1, billing_suspend_after_days=3).run_once(
        now=NOW
    )

    assert subscription.status is SubscriptionStatus.SUSPENDED


def test_a_hard_threshold_before_the_soft_one_is_refused() -> None:
    """Configuration that would suspend somebody before telling them anything.

    Refused in every environment, `test` included: this is an ordering rather
    than a credential, and an ordering that is wrong is wrong everywhere.
    """
    with pytest.raises(ValueError, match="BILLING_SUSPEND_AFTER_DAYS"):
        _settings(billing_past_due_days=7, billing_suspend_after_days=7)
    with pytest.raises(ValueError, match="BILLING_SUSPEND_AFTER_DAYS"):
        _settings(billing_past_due_days=10, billing_suspend_after_days=3)


async def test_a_suspended_workspace_cannot_change_its_way_out_of_the_bill(
    db_session: AsyncSession,
) -> None:
    """Refused, and told the truth about why.

    Cancelling, resuming or downgrading to the free tier would all be ways to
    escape an unpaid invoice, so a suspended subscription refuses each - which
    it already did, because `SUSPENDED` is terminal. What this also asserts is
    the message: three call sites used to say "this subscription has ended",
    which is false for a workspace that owes money and would send somebody down
    a path that does not fix their problem.
    """
    from app.core.exceptions import ConflictError
    from app.services.subscription_service import SubscriptionService

    tenant = await _tenant(db_session)
    free = await _free_plan(db_session)
    paid = await _paid_plan(db_session)
    await _subscription(db_session, tenant, paid, status=SubscriptionStatus.SUSPENDED)
    service = SubscriptionService(db_session, tenant_id=tenant.id, settings=_settings())

    for call in (
        lambda: service.change_plan(plan_code=free.code, now=NOW),
        lambda: service.cancel(now=NOW),
        lambda: service.resume(),
    ):
        with pytest.raises(ConflictError) as caught:
            await call()
        assert "suspended" in str(caught.value).lower()
        assert "unpaid invoice" in str(caught.value)
