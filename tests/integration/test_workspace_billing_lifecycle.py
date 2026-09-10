"""Deleting a workspace must stop the money, and must not destroy the records.

One invariant: **a customer is never charged for a renewal belonging to a
workspace they closed.** Two things enforce it, and this file tests both,
because either alone is a single point of failure.

*At deletion*, `WorkspaceService._wind_down_billing` cancels the subscription
immediately and revokes every saved card, in the same transaction as the
tombstone.

*At the sweep*, `PlatformSubscriptionRepository.claim_due` and
`PlatformInvoiceRepository.claim_collectible` - the cross-workspace reads the
billing worker actually runs - both exclude deleted workspaces - so a
workspace tombstoned by an operator's SQL, by a restore from an older backup, or
by a future path that forgets to cancel still cannot be charged.

The second half is worth stating plainly, because it shapes what "provider
cancellation" means here. **There is no provider-side subscription object.**
`Subscription.provider_reference` is declared and written by nothing, and the
Paymob client exposes checkout, a saved-card charge, an inquiry and a refund -
no subscription resource and no cancellation endpoint. Wasla *is* the recurring
engine: the sweep issues an invoice and `RecurringService` debits a stored
token. The thing that can still take money is therefore the token, and revoking
it is the cancellation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import hash_password
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import TenantStatus
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.main import create_app
from app.repositories.billing_repository import (
    PlatformSubscriptionRepository,
    SubscriptionRepository,
)
from app.repositories.invoice_repository import PlatformInvoiceRepository
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def billing_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="",
    )


@pytest.fixture
def app(billing_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(billing_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
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


async def _owner(session: AsyncSession, email: str) -> User:
    user = User(
        email=email,
        full_name="Owner",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=NOW,
    )
    session.add(user)
    await session.flush()
    return user


async def _plan(session: AsyncSession, *, code: str, price: str) -> Plan:
    existing = await session.scalar(select(Plan).where(Plan.code == code))
    if existing is not None:
        return existing
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal(price),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _workspace(
    session: AsyncSession,
    *,
    slug: str,
    owner: User,
    plan: Plan | None = None,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER))
    if plan is not None:
        session.add(
            Subscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                status=status,
                current_period_start=NOW - timedelta(days=15),
                current_period_end=NOW + timedelta(days=15),
            )
        )
    await session.flush()
    return tenant


async def _card(session: AsyncSession, tenant: Tenant) -> PaymentMethod:
    method = PaymentMethod(
        tenant_id=tenant.id,
        provider="paymob",
        provider_token="tok_" + tenant.slug,
        status=PaymentMethodStatus.ACTIVE,
        is_default=True,
    )
    session.add(method)
    await session.flush()
    return method


async def _login(http: AsyncClient, email: str, slug: str) -> dict[str, str]:
    response = await http.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    session: dict[str, Any] = response.json()
    switched = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": slug},
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert switched.status_code == 200, switched.text
    return {"Authorization": f"Bearer {switched.json()['access_token']}"}


async def _delete(http: AsyncClient, headers: dict[str, str], slug: str) -> Any:
    return await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": slug},
        headers=headers,
    )


# ---------------------------------------------------------------- deletion


async def test_deleting_a_free_workspace_cancels_its_subscription(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Free is not a special case, and treating it as one is how paid breaks.

    A free plan's subscription still rolls periods over and still resolves
    entitlements, so leaving it serving on a deleted workspace would leave the
    sweep with work to do for a workspace nobody can open.
    """
    owner = await _owner(db_session, "free@example.com")
    plan = await _plan(db_session, code="free-tier", price="0.00")
    tenant = await _workspace(db_session, slug="free-co", owner=owner, plan=plan)

    headers = await _login(http, owner.email, "free-co")
    assert (await _delete(http, headers, "free-co")).status_code == 200

    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.ended_at is not None


async def test_deleting_a_paid_workspace_cancels_immediately_and_revokes_the_card(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The invariant, at the moment of deletion.

    Immediate rather than at period end. `cancel_at_period_end` is the right
    default when somebody merely stops wanting a plan - they keep what they paid
    for and the workspace stays open. Deletion is different: access ends now, so
    a subscription left serving until the boundary would be a live subscription
    attached to a workspace nobody can open, and the sweep would roll it over on
    schedule.

    The card is the other half. There is no provider-side subscription to
    cancel; the thing that can still take money is the stored token, so revoking
    it *is* the cancellation.
    """
    owner = await _owner(db_session, "paid@example.com")
    plan = await _plan(db_session, code="paid-tier", price="99.00")
    tenant = await _workspace(db_session, slug="paid-co", owner=owner, plan=plan)
    card = await _card(db_session, tenant)

    headers = await _login(http, owner.email, "paid-co")
    assert (await _delete(http, headers, "paid-co")).status_code == 200

    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.cancel_at_period_end is False
    # The period is closed too, so nothing counts against an allowance the
    # workspace no longer has.
    assert subscription.current_period_end <= datetime.now(UTC)

    await db_session.refresh(card)
    assert card.status is PaymentMethodStatus.REVOKED
    assert card.revoked_at is not None
    assert card.is_default is False


async def test_the_deletion_audit_entry_records_what_happened_to_the_money(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An operator reading the trail must be able to tell paid from free."""
    owner = await _owner(db_session, "audited@example.com")
    plan = await _plan(db_session, code="audited-tier", price="49.00")
    tenant = await _workspace(db_session, slug="audited-co", owner=owner, plan=plan)
    await _card(db_session, tenant)

    headers = await _login(http, owner.email, "audited-co")
    assert (await _delete(http, headers, "audited-co")).status_code == 200

    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.WORKSPACE_DELETED)
    )
    entry = next(iter(rows.scalars()))
    assert entry.meta is not None
    assert entry.meta["subscription_cancelled"] is True
    assert entry.meta["payment_methods_revoked"] == 1
    # And the cancellation is in the trail in its own right.
    cancelled = await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.SUBSCRIPTION_CANCELLED)
    )
    assert list(cancelled.scalars())


async def test_an_already_cancelled_subscription_does_not_block_deletion(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`SubscriptionService.cancel` refuses a terminal subscription.

    Calling it unconditionally would mean a workspace whose plan had already
    expired could not be deleted at all - the customer would be locked into a
    workspace they had stopped paying for.
    """
    owner = await _owner(db_session, "expired@example.com")
    plan = await _plan(db_session, code="expired-tier", price="10.00")
    tenant = await _workspace(
        db_session,
        slug="expired-co",
        owner=owner,
        plan=plan,
        status=SubscriptionStatus.CANCELLED,
    )

    headers = await _login(http, owner.email, "expired-co")
    response = await _delete(http, headers, "expired-co")

    assert response.status_code == 200, response.text
    await db_session.refresh(tenant)
    assert tenant.deleted_at is not None


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.PAST_DUE, SubscriptionStatus.SUSPENDED, SubscriptionStatus.TRIALING],
)
async def test_deletion_works_from_every_subscription_state(
    http: AsyncClient,
    db_session: AsyncSession,
    status: SubscriptionStatus,
) -> None:
    """A customer must be able to leave from any billing state.

    `PAST_DUE` in particular: somebody whose card failed and who has decided to
    go should not find that the failed payment is what traps them.
    """
    owner = await _owner(db_session, f"state-{status.value}@example.com")
    plan = await _plan(db_session, code="state-tier", price="20.00")
    slug = f"state-{status.value.replace('_', '-')}-co"
    tenant = await _workspace(db_session, slug=slug, owner=owner, plan=plan, status=status)

    headers = await _login(http, owner.email, slug)
    assert (await _delete(http, headers, slug)).status_code == 200

    await db_session.refresh(tenant)
    assert tenant.deleted_at is not None
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    assert not subscription.is_serving


async def test_deleting_one_workspace_leaves_another_ones_billing_alone(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The cross-tenant assertion. A subscription belongs to one workspace."""
    owner = await _owner(db_session, "two-businesses@example.com")
    plan = await _plan(db_session, code="shared-tier", price="30.00")
    doomed = await _workspace(db_session, slug="doomed-billing", owner=owner, plan=plan)
    kept = await _workspace(db_session, slug="kept-billing", owner=owner, plan=plan)
    kept_card = await _card(db_session, kept)
    await _card(db_session, doomed)

    headers = await _login(http, owner.email, "doomed-billing")
    assert (await _delete(http, headers, "doomed-billing")).status_code == 200

    kept_subscription = await SubscriptionRepository(db_session, tenant_id=kept.id).get()
    assert kept_subscription is not None
    assert kept_subscription.status is SubscriptionStatus.ACTIVE
    await db_session.refresh(kept_card)
    assert kept_card.status is PaymentMethodStatus.ACTIVE


async def test_invoices_and_payments_survive_deletion(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Financial history is not the customer's to delete, or ours.

    A tax authority, a chargeback and a reconciliation against the processor's
    ledger all arrive after somebody leaves, and all of them need the row.
    """
    owner = await _owner(db_session, "history@example.com")
    plan = await _plan(db_session, code="history-tier", price="75.00")
    tenant = await _workspace(db_session, slug="history-co", owner=owner, plan=plan)
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.PAID,
        plan_code=plan.code,
        amount_due=Decimal("75.00"),
        amount_paid=Decimal("75.00"),
        currency="EGP",
        period_start=NOW - timedelta(days=30),
        period_end=NOW,
        lines=[],
    )
    db_session.add(invoice)
    await db_session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        amount=Decimal("75.00"),
        currency="EGP",
        status=PaymentStatus.SUCCEEDED,
        provider="paymob",
        provider_reference="txn-history-1",
    )
    db_session.add(payment)
    await db_session.flush()

    headers = await _login(http, owner.email, "history-co")
    assert (await _delete(http, headers, "history-co")).status_code == 200

    await db_session.refresh(invoice)
    await db_session.refresh(payment)
    assert invoice.status is InvoiceStatus.PAID
    assert payment.provider_reference == "txn-history-1"


# ------------------------------------------------------------- the sweep


async def test_the_renewal_sweep_will_not_claim_a_deleted_workspace(
    db_session: AsyncSession,
) -> None:
    """Defence in depth, and the reason it is not redundant.

    Deletion cancels the subscription, so in the ordinary case this filter never
    fires. It exists because "never charged after closing" is an invariant
    rather than a step - and here the subscription is deliberately left ACTIVE
    and due, which is the state an operator's SQL, an older backup or a future
    deletion path that forgot to cancel would produce.
    """
    owner = await _owner(db_session, "sweep@example.com")
    plan = await _plan(db_session, code="sweep-tier", price="15.00")
    tenant = await _workspace(db_session, slug="sweep-co", owner=owner, plan=plan)
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    subscription.current_period_end = NOW - timedelta(days=1)
    await db_session.flush()

    # The *platform* repository: the sweep reads across every workspace, so
    # this is the class that actually runs in the billing worker.
    repository = PlatformSubscriptionRepository(db_session)
    claimed = await repository.claim_due(now=NOW)
    assert subscription.id in [row.id for row in claimed]

    # Tombstoned by hand, subscription deliberately untouched.
    tenant.deleted_at = NOW
    await db_session.flush()

    assert subscription.id not in [row.id for row in await repository.claim_due(now=NOW)]


async def test_automatic_collection_will_not_claim_a_deleted_workspaces_invoice(
    db_session: AsyncSession,
) -> None:
    """The query immediately in front of a card debit, so the last place to be sure."""
    owner = await _owner(db_session, "collect@example.com")
    plan = await _plan(db_session, code="collect-tier", price="45.00")
    tenant = await _workspace(db_session, slug="collect-co", owner=owner, plan=plan)
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("45.00"),
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=NOW - timedelta(days=30),
        period_end=NOW,
        lines=[],
    )
    db_session.add(invoice)
    await db_session.flush()

    invoices = PlatformInvoiceRepository(db_session)
    claimed = await invoices.claim_collectible(before=NOW, max_attempts=3)
    assert invoice.id in [row.id for row in claimed]

    tenant.deleted_at = NOW
    await db_session.flush()

    remaining = await invoices.claim_collectible(before=NOW, max_attempts=3)
    assert invoice.id not in [row.id for row in remaining]
