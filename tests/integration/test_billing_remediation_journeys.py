"""Billing remediation regressions: the audit's journeys, end to end over time.

The audit's central observation (structural gap 4) was that billing was tested
per step and never as a customer's life: nothing paid after the Starter trial
ended, nothing bought and then ran two renewals and summed the bills, nothing
opened two checkouts before paying one. Every test here drives the real
services - checkout, signed Paymob callbacks through the real adapter, the
billing worker, the settlement engine - against real PostgreSQL, across time.

Paymob is faked at the socket only. Its intention responses carry an order id
derived from our reference, exactly as `tests/paymob_orders.py` explains, so
callbacks are bound the way production binds them.
"""

from __future__ import annotations

import itertools
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.billing import LimitKey, Subscription, SubscriptionStatus
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.enums import TenantStatus
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.payment_method import PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.platform.plan_admin import PlanCatalogAdmin
from app.repositories.invoice_repository import PlatformInvoiceRepository
from app.schemas.platform_billing import PlanMigrationCreate, PlanVersionCreate
from app.services.checkout_service import (
    APPLIED,
    DUPLICATE,
    MISMATCHED,
    CheckoutService,
)
from app.services.entitlement_service import EntitlementService
from app.services.invoice_service import InvoiceService
from app.services.recurring_service import (
    MAX_COLLECTION_ATTEMPTS,
    PERMANENT_FAILURE,
    RecurringService,
)
from app.services.subscription_service import SubscriptionService
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.billing_fixtures import add_owner
from tests.fakes import as_database
from tests.integration.plan_catalogue import own_plan
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY, PROTECTOR, saved_card
from tests.paymob_orders import CARD_INTEGRATION_ID, MOTO_INTEGRATION_ID, order_from_request

pytestmark = pytest.mark.integration

HMAC_SECRET = "journey-hmac-secret"
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


class Paymob:
    """Paymob at the socket: intentions, MIT pay requests, and a scripted inquiry."""

    def __init__(self, *, intention_status: int = 201) -> None:
        self.requests: list[dict[str, Any]] = []
        self.intention_status = intention_status
        self.next_transaction = 700_000_001
        self.inquiry: dict[str, Any] | None = None
        self.card_tokens: list[dict[str, Any]] = []

    def pays(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if item["url"].endswith("/payments/pay")]

    def intentions(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if "/v1/intention" in item["url"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append({"url": str(request.url), "body": body})
        url = str(request.url)
        if "/v1/intention" in url:
            if self.intention_status != 201:
                return httpx.Response(self.intention_status, json={"detail": "refused"})
            return httpx.Response(
                201,
                json={
                    "id": f"pi_test_{uuid.uuid4().hex[:12]}",
                    "client_secret": f"egy_csk_test_{uuid.uuid4().hex[:12]}",
                    "intention_order_id": order_from_request(request),
                    "payment_keys": [{"key": "a-moto-payment-key"}],
                },
            )
        if url.endswith("/payments/pay"):
            self.next_transaction += 1
            return httpx.Response(
                200, json={"id": self.next_transaction, "pending": True, "success": False}
            )
        if url.endswith("/api/auth/tokens"):
            return httpx.Response(201, json={"token": "inquiry-bearer"})
        if url.endswith("/transaction_inquiry"):
            return httpx.Response(200, json=self.inquiry or {})
        if url.endswith("/order_card_tokens"):
            return httpx.Response(200, json=self.card_tokens)
        return httpx.Response(404, json={})

    def provider(self, *, moto: bool = True, api_key: str | None = None) -> PaymobProvider:
        return PaymobProvider(
            secret_key="egy_sk_test_journey",
            public_key="egy_pk_test_journey",
            hmac_secret=HMAC_SECRET,
            integration_ids=[CARD_INTEGRATION_ID],
            moto_integration_id=MOTO_INTEGRATION_ID if moto else None,
            api_key=api_key,
            transport=httpx.MockTransport(self.handler),
        )


def _scheduled(subscription: Subscription) -> bool:
    """Read afresh, so an earlier assertion does not narrow what a worker changed."""
    return subscription.has_scheduled_change


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        jwt_secret=secrets.token_urlsafe(32),
        credential_encryption_keys=[ENCRYPTION_KEY],
        payment_token_fingerprint_key=FINGERPRINT_KEY,
        default_plan_code="starter",
    )


class SessionHandle:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


def _worker(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, provider: PaymobProvider
) -> BillingWorker:
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)
    return BillingWorker(database=as_database(SessionHandle(session)), settings=_settings())


async def _catalogue(session: AsyncSession) -> None:
    await own_plan(session, code="starter", price=Decimal("0.00"), limits={"agents": 1})
    await own_plan(session, code="pro", price=Decimal("99.00"), limits={"agents": 5})
    await own_plan(session, code="business", price=Decimal("299.00"), limits={"agents": 20})


async def _workspace(
    session: AsyncSession, *, now: datetime = T0, catalogue: bool = True
) -> tuple[Tenant, User]:
    """A workspace as registration leaves it: an owner, on Starter."""
    if catalogue:
        await _catalogue(session)
    tenant = Tenant(name="Journey", slug=f"journey-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await add_owner(session, tenant)
    await SubscriptionService(session, tenant_id=tenant.id).start(
        plan_code="starter", now=now, self_service=False
    )
    return tenant, owner


def _callback(
    payment: Payment,
    *,
    transaction: int,
    success: bool = True,
    integration: int | None = None,
    is_live: bool = False,
    reference: str | None = None,
    order: str | None = None,
) -> tuple[bytes, str]:
    obj = {
        "id": transaction,
        "pending": False,
        "amount_cents": int(payment.amount * 100),
        "success": success,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": (
            integration
            if integration is not None
            else (MOTO_INTEGRATION_ID if payment.is_automatic else CARD_INTEGRATION_ID)
        ),
        "has_parent_transaction": False,
        "order": {
            "id": int(order if order is not None else str(payment.provider_order_id)),
            "merchant_order_id": reference if reference is not None else str(payment.id),
        },
        "is_live": is_live,
        "created_at": "2026-09-01T10:00:00.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": not success,
        "owner": 302852,
    }
    body = json.dumps({"type": "TRANSACTION", "obj": obj}).encode("utf-8")
    return body, hmac_signature(obj, secret=HMAC_SECRET)


async def _apply(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    provider: PaymobProvider,
    signed: tuple[bytes, str],
    *,
    now: datetime,
) -> str:
    event = provider.verify_callback(payload=signed[0], signature=signed[1])
    return await CheckoutService(
        session, tenant_id=tenant_id, provider=provider, default_plan_code="starter"
    ).apply(event, now=now)


async def _buy(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    paymob: Paymob,
    plan_code: str,
    *,
    now: datetime,
    transaction: int,
) -> tuple[Invoice, Payment]:
    provider = paymob.provider()
    started = await CheckoutService(session, tenant_id=tenant.id, provider=provider).start(
        plan_code=plan_code, actor=owner, now=now
    )
    payment = await session.get(Payment, started.payment_id)
    invoice = await session.get(Invoice, started.invoice_id)
    assert payment is not None and invoice is not None
    assert (
        await _apply(
            session, tenant.id, provider, _callback(payment, transaction=transaction), now=now
        )
        == APPLIED
    )
    return invoice, payment


async def _subscription(session: AsyncSession, tenant: Tenant) -> Subscription:
    row = await session.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id))
    assert row is not None
    return row


async def _agents(session: AsyncSession, tenant: Tenant) -> int | None:
    service = EntitlementService(session, tenant_id=tenant.id, default_plan_code="starter")
    return (await service.check(LimitKey.AGENTS)).limit


# --------------------------------------------------------------- BILL-01


async def test_starter_is_permanently_free_and_a_later_purchase_is_granted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BILL-01. Starter no longer expires on day fourteen; buying Pro on day sixty works.

    The audit reproduced the defect with real money: an "expired" Starter
    workspace paid 99 EGP and kept Starter.
    """
    tenant, owner = await _workspace(db_session)
    subscription = await _subscription(db_session, tenant)
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.trial_ends_at is None

    paymob = Paymob()
    worker = _worker(db_session, monkeypatch, paymob.provider())
    for months in (1, 2):
        await worker.run_once(now=T0 + timedelta(days=31 * months))
    assert subscription.status is SubscriptionStatus.ACTIVE, "a free plan never expires"

    day60 = T0 + timedelta(days=60, hours=1)
    invoice, payment = await _buy(
        db_session, tenant, owner, paymob, "pro", now=day60, transaction=700_100_001
    )

    assert invoice.status is InvoiceStatus.PAID
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert await _agents(db_session, tenant) == 5
    assert (subscription.current_period_start, subscription.current_period_end) == (
        invoice.period_start,
        invoice.period_end,
    )


# --------------------------------------------------------------- BILL-02


async def test_an_abandoned_upgrade_checkout_is_never_charged_by_the_sweep(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BILL-02. A Pro customer with a saved card opens Business and walks away.

    The audit's probe P5: within ten minutes the sweep debited 299 EGP from the
    saved card and moved the workspace to Business. A checkout is the
    customer's to pay at a page; it is never a merchant-initiated charge.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, "pro", now=T0, transaction=700_200_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    abandoned = await CheckoutService(
        db_session, tenant_id=tenant.id, provider=paymob.provider()
    ).start(plan_code="business", actor=owner, now=T0 + timedelta(hours=1))
    paymob.requests.clear()

    worker = _worker(db_session, monkeypatch, paymob.provider())
    for hours in (2, 24, 72):
        await worker.run_once(now=T0 + timedelta(hours=hours))

    assert paymob.pays() == [], "an abandoned checkout was charged to the saved card"
    assert paymob.intentions() == []
    claimable = await PlatformInvoiceRepository(db_session).claim_collectible(
        before=T0 + timedelta(days=3), max_attempts=MAX_COLLECTION_ATTEMPTS
    )
    assert abandoned.invoice_id not in {row.id for row in claimable}
    assert await _agents(db_session, tenant) == 5, "still Pro"


# --------------------------------------------------------------- BILL-03


async def test_every_paid_period_is_billed_exactly_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BILL-03. Buy Pro, then renew twice: three adjacent periods, three bills.

    The audit's real Paymob run charged 99 EGP twice for Sep 24 -> Oct 24: the
    checkout billed it in advance and the sweep billed it again in arrears.
    Renewals now bill the *next* period; the MIT for it is charged once, its
    callback replayed changes nothing, and the period advances once.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    purchase, _ = await _buy(
        db_session, tenant, owner, paymob, "pro", now=T0, transaction=700_300_001
    )
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    subscription = await _subscription(db_session, tenant)
    provider = paymob.provider()
    worker = _worker(db_session, monkeypatch, provider)

    for step in (1, 2):
        boundary = subscription.current_period_end
        moment = boundary + timedelta(minutes=5)
        await worker.run_once(now=moment)
        charge = (
            await db_session.scalars(
                select(Payment)
                .where(Payment.tenant_id == tenant.id)
                .where(Payment.is_automatic.is_(True))
                .where(Payment.collection_state == CollectionState.REQUESTED)
            )
        ).one()
        signed = _callback(charge, transaction=700_300_100 + step)
        assert await _apply(db_session, tenant.id, provider, signed, now=moment) == APPLIED
        assert await _apply(db_session, tenant.id, provider, signed, now=moment) == DUPLICATE
        assert charge.collection_state is CollectionState.SETTLED
        # The period advanced exactly once for the one charge.
        assert subscription.current_period_start == boundary

    invoices = (
        await db_session.scalars(
            select(Invoice)
            .where(Invoice.tenant_id == tenant.id)
            .where(Invoice.plan_code == "pro")
            .order_by(Invoice.period_start)
        )
    ).all()
    assert [invoice.purpose for invoice in invoices] == [
        InvoicePurpose.CHECKOUT,
        InvoicePurpose.RENEWAL,
        InvoicePurpose.RENEWAL,
    ]
    assert all(invoice.status is InvoiceStatus.PAID for invoice in invoices)
    assert invoices[0].id == purchase.id
    for earlier, later in itertools.pairwise(invoices):
        assert earlier.period_end == later.period_start, "periods must be adjacent"
    assert sum(invoice.amount_paid for invoice in invoices) == Decimal("297.00")
    assert len(paymob.pays()) == 2, "one charge per renewal"


# --------------------------------------------------------------- BILL-06


async def test_a_cheaper_page_buys_only_the_cheaper_plan(db_session: AsyncSession) -> None:
    """BILL-06. Open Pro (99), open Business (299), pay the Pro page -> Pro.

    The second checkout used to re-price the shared invoice, so 99 EGP bought
    Business - with the shortfall never dunned. Each checkout is its own frozen
    invoice now; paying Business later is its own upgrade.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    provider = paymob.provider()
    service = CheckoutService(db_session, tenant_id=tenant.id, provider=provider)
    pro = await service.start(plan_code="pro", actor=owner, now=T0)
    business = await service.start(plan_code="business", actor=owner, now=T0 + timedelta(minutes=1))

    pro_payment = await db_session.get(Payment, pro.payment_id)
    assert pro_payment is not None
    later = T0 + timedelta(minutes=2)
    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(pro_payment, transaction=700_600_001),
            now=later,
        )
        == APPLIED
    )

    assert await _agents(db_session, tenant) == 5, "Pro, never Business"
    business_invoice = await db_session.get(Invoice, business.invoice_id)
    assert business_invoice is not None
    assert business_invoice.status is InvoiceStatus.OPEN
    assert business_invoice.amount_due == Decimal("299.00")

    business_payment = await db_session.get(Payment, business.payment_id)
    assert business_payment is not None
    await _apply(
        db_session,
        tenant.id,
        provider,
        _callback(business_payment, transaction=700_600_002),
        now=later + timedelta(minutes=1),
    )
    assert await _agents(db_session, tenant) == 20


async def test_a_checkout_settles_on_the_terms_it_was_opened_at(db_session: AsyncSession) -> None:
    """BILL-06, mutation B17. A new version published while the page is open changes nothing.

    The customer opened Pro at 99 EGP / 5 agents. Before they paid, an operator
    published Pro v2 at 149 / 3 agents. The 99 they pay buys what the page
    showed them: v1, five agents - never the terms that happened to be current
    when the callback arrived.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    provider = paymob.provider()
    started = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).start(
        plan_code="pro", actor=owner, now=T0
    )
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None and invoice.plan_version_id is not None
    opened_at = invoice.plan_version_id

    pro = await own_plan(db_session, code="pro", price=Decimal("99.00"), limits={"agents": 5})
    admin = PlanCatalogAdmin(db_session)
    latest = (await admin.versions(pro.id))[0].version
    await admin.create_version(
        pro.id,
        PlanVersionCreate(
            price=Decimal("149.00"),
            currency="EGP",
            interval="monthly",
            limits={"agents": 3},
            expected_version=latest,
            reason="Repriced while a customer's page was open.",
        ),
        actor=await add_owner(db_session, tenant),
        now=T0 + timedelta(minutes=1),
    )

    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(payment, transaction=700_600_101),
            now=T0 + timedelta(minutes=2),
        )
        == APPLIED
    )

    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_version_id == opened_at
    assert await _agents(db_session, tenant) == 5
    assert invoice.amount_paid == Decimal("99.00")


# --------------------------------------------------------------- BILL-11


async def test_a_signed_callback_cannot_be_re_aimed_or_cross_environments(
    db_session: AsyncSession,
) -> None:
    """BILL-11. The unsigned reference, the integration and the mode are all bound.

    Paymob does not sign `merchant_order_id`, so a validly signed body could be
    edited to name another same-amount payment. Settlement is now keyed on the
    signed `order.id`, and the integration and `is_live` must match this
    deployment. Every refusal is recorded as a mismatched-callback incident.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    provider = paymob.provider()
    service = CheckoutService(db_session, tenant_id=tenant.id, provider=provider)
    first = await service.start(plan_code="pro", actor=owner, now=T0)
    second = await service.start(plan_code="business", actor=owner, now=T0)
    victim = await db_session.get(Payment, first.payment_id)
    other = await db_session.get(Payment, second.payment_id)
    assert victim is not None and other is not None

    retargeted = _callback(victim, transaction=700_110_001, reference=str(other.id))
    wrong_integration = _callback(victim, transaction=700_110_002, integration=999_999)
    live_on_test = _callback(victim, transaction=700_110_003, is_live=True)
    moto_on_hosted = _callback(victim, transaction=700_110_004, integration=MOTO_INTEGRATION_ID)

    for signed in (retargeted, wrong_integration, live_on_test, moto_on_hosted):
        assert await _apply(db_session, tenant.id, provider, signed, now=T0) == MISMATCHED

    assert victim.status is PaymentStatus.PENDING
    assert other.status is PaymentStatus.PENDING
    incidents = (
        await db_session.scalars(
            select(BillingIncident.kind).where(BillingIncident.tenant_id == tenant.id)
        )
    ).all()
    assert incidents == [BillingIncidentKind.MISMATCHED_CALLBACK] * 4

    genuine = _callback(victim, transaction=700_110_005)
    assert await _apply(db_session, tenant.id, provider, genuine, now=T0) == APPLIED


# --------------------------------------------------------------- BILL-05


async def test_an_automatic_charge_sends_the_owners_real_email(db_session: AsyncSession) -> None:
    """BILL-05. The MOTO intention carries the billing owner's address, never a placeholder."""
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, "pro", now=T0, transaction=700_500_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    subscription = await _subscription(db_session, tenant)
    renewal_at = subscription.current_period_end + timedelta(minutes=1)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=renewal_at)
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    paymob.requests.clear()
    outcome = await RecurringService(
        db_session, tenant_id=tenant.id, provider=paymob.provider(), payment_tokens=PROTECTOR
    ).collect(renewal, subscription=subscription, now=renewal_at)

    assert outcome.charged
    billing = paymob.intentions()[0]["body"]["billing_data"]
    assert billing["email"] == owner.email
    assert "NOT_COLLECTED" not in json.dumps(billing)


async def test_a_permanent_intention_refusal_stops_automatic_collection(
    db_session: AsyncSession,
) -> None:
    """BILL-05. A 4xx on the MOTO intention is permanent: stop, spend the budget, alert.

    It used to be filed as "not sent", the attempt returned, and the same
    refused request retried every day for ever with nobody told.
    """
    tenant, owner = await _workspace(db_session)
    good = Paymob()
    await _buy(db_session, tenant, owner, good, "pro", now=T0, transaction=700_510_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    subscription = await _subscription(db_session, tenant)
    renewal_at = subscription.current_period_end + timedelta(minutes=1)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=renewal_at)
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None

    refusing = Paymob(intention_status=400)
    outcome = await RecurringService(
        db_session, tenant_id=tenant.id, provider=refusing.provider(), payment_tokens=PROTECTOR
    ).collect(renewal, subscription=subscription, now=renewal_at)

    assert outcome.reason == PERMANENT_FAILURE
    assert refusing.pays() == []
    assert renewal.collection_attempts == MAX_COLLECTION_ATTEMPTS
    assert renewal.next_collection_at is None
    incident = await db_session.scalar(
        select(BillingIncident).where(BillingIncident.invoice_id == renewal.id)
    )
    assert incident is not None
    assert incident.kind is BillingIncidentKind.PERMANENT_PROVIDER_ERROR
    claimable = await PlatformInvoiceRepository(db_session).claim_collectible(
        before=renewal_at + timedelta(days=30), max_attempts=MAX_COLLECTION_ATTEMPTS
    )
    assert renewal.id not in {row.id for row in claimable}


# --------------------------------------------------------------- BILL-16


async def test_the_first_abandonment_backs_off_fifteen_minutes_without_autoflush(
    db_session: AsyncSession,
) -> None:
    """BILL-16. Production sessions do not autoflush; the backoff must not depend on it."""
    tenant, owner = await _workspace(db_session)
    await _buy(db_session, tenant, owner, Paymob(), "pro", now=T0, transaction=700_160_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    subscription = await _subscription(db_session, tenant)
    renewal_at = subscription.current_period_end + timedelta(minutes=1)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=renewal_at)
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None

    db_session.sync_session.autoflush = False
    try:
        outcome = await RecurringService(
            db_session,
            tenant_id=tenant.id,
            provider=Paymob(intention_status=503).provider(),
            payment_tokens=PROTECTOR,
        ).collect(renewal, subscription=subscription, now=renewal_at)
    finally:
        db_session.sync_session.autoflush = True

    assert outcome.reason == "not_sent"
    assert renewal.next_collection_at == renewal_at + timedelta(minutes=15)


# --------------------------------------------------------------- BILL-17


async def test_a_platform_suspended_workspace_is_never_charged(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BILL-17. The owner cannot sign in to cancel; the card must not be debited."""
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, "pro", now=T0, transaction=700_170_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()
    subscription = await _subscription(db_session, tenant)
    paymob.requests.clear()

    worker = _worker(db_session, monkeypatch, paymob.provider())
    await worker.run_once(now=subscription.current_period_end + timedelta(minutes=5))
    await worker.run_once(now=subscription.current_period_end + timedelta(days=40))

    assert paymob.pays() == []
    assert paymob.intentions() == []
    assert subscription.status is SubscriptionStatus.ACTIVE, "no dunning while held either"


# ------------------------------------------------------- versions (BILL-12)


async def test_a_price_change_reaches_new_customers_and_not_existing_ones(
    db_session: AsyncSession,
) -> None:
    """Spec 119/120: Pro v1 99 / 5 agents; Pro v2 149 / 3 agents.

    Subscriber A (on v1) keeps 99 and 5 agents; subscriber B pays 149 and gets
    3. One `UPDATE plans` used to re-price and downgrade everybody at once.
    """
    a_tenant, a_owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, a_tenant, a_owner, paymob, "pro", now=T0, transaction=700_120_001)
    a_subscription = await _subscription(db_session, a_tenant)

    pro = await own_plan(db_session, code="pro", price=Decimal("99.00"), limits={"agents": 5})
    staff = await add_owner(db_session, a_tenant)
    admin = PlanCatalogAdmin(db_session)
    latest = (await admin.versions(pro.id))[0].version
    await admin.create_version(
        pro.id,
        PlanVersionCreate(
            price=Decimal("149.00"),
            currency="EGP",
            interval="monthly",
            limits={"agents": 3},
            expected_version=latest,
            reason="New price for new customers.",
        ),
        actor=staff,
        now=T0 + timedelta(days=1),
    )

    b_tenant, b_owner = await _workspace(db_session, now=T0 + timedelta(days=2), catalogue=False)
    b_invoice, _ = await _buy(
        db_session,
        b_tenant,
        b_owner,
        paymob,
        "pro",
        now=T0 + timedelta(days=2),
        transaction=700_120_002,
    )

    assert b_invoice.amount_due == Decimal("149.00")
    assert await _agents(db_session, a_tenant) == 5
    assert await _agents(db_session, b_tenant) == 3

    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=a_subscription.current_period_end + timedelta(minutes=1))
    a_renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == a_tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert a_renewal is not None
    assert a_renewal.amount_due == Decimal("99.00"), "A renews on the version A holds"


async def test_a_scheduled_migration_moves_a_subscriber_only_when_the_renewal_is_paid(
    db_session: AsyncSession,
) -> None:
    """Spec 121: A on v1 (99); migration v1 -> v2 (149) at next renewal.

    Before the renewal A stays on v1. At the renewal the invoice is 149 for v2;
    while it is unpaid A keeps v1's entitlements; paying it moves A to v2.
    """
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, "pro", now=T0, transaction=700_121_001)
    subscription = await _subscription(db_session, tenant)
    v1 = subscription.plan_version_id

    pro = await own_plan(db_session, code="pro", price=Decimal("99.00"), limits={"agents": 5})
    admin = PlanCatalogAdmin(db_session)
    versions = await admin.versions(pro.id)
    current = next(v for v in versions if v.id == v1)
    v2 = await admin.create_version(
        pro.id,
        PlanVersionCreate(
            price=Decimal("149.00"),
            currency="EGP",
            interval="monthly",
            limits={"agents": 8},
            expected_version=versions[0].version,
            reason="Repricing.",
        ),
        actor=owner,
        now=T0 + timedelta(days=1),
    )
    preview = await admin.schedule_migration(
        pro.id,
        PlanMigrationCreate(
            from_version=current.version, to_version=v2.version, reason="Move to v2."
        ),
        actor=owner,
    )
    assert preview.scheduled is False and preview.affected_subscriptions >= 1
    scheduled = await admin.schedule_migration(
        pro.id,
        PlanMigrationCreate(
            from_version=current.version, to_version=v2.version, reason="Move to v2.", confirm=True
        ),
        actor=owner,
    )
    assert scheduled.scheduled is True
    assert subscription.plan_version_id == v1

    boundary = subscription.current_period_end
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=boundary + timedelta(minutes=1))
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    assert renewal.amount_due == Decimal("149.00")
    assert renewal.plan_version_id == v2.id
    assert subscription.plan_version_id == v1, "not before the renewal is paid"
    assert await _agents(db_session, tenant) == 5

    await InvoiceService(db_session, tenant_id=tenant.id).record_payment(
        invoice_id=renewal.id,
        amount=Decimal("149.00"),
        provider="bank_transfer",
        reference="migration-renewal",
        now=boundary + timedelta(hours=1),
        currency="EGP",
    )
    assert subscription.plan_version_id == v2.id
    assert await _agents(db_session, tenant) == 8


async def test_a_downgrade_waits_for_the_period_already_paid_for(
    db_session: AsyncSession,
) -> None:
    """Spec 14: Business -> Pro takes effect at the period end, billed at Pro's price."""
    tenant, owner = await _workspace(db_session)
    await _buy(db_session, tenant, owner, Paymob(), "business", now=T0, transaction=700_140_001)
    subscription = await _subscription(db_session, tenant)

    await SubscriptionService(db_session, tenant_id=tenant.id).request_plan(
        plan_code="pro", now=T0 + timedelta(days=3), actor=owner
    )
    assert subscription.has_scheduled_change
    assert await _agents(db_session, tenant) == 20, "the paid period is kept"

    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=subscription.current_period_end + timedelta(minutes=1))
    assert not _scheduled(subscription)
    assert await _agents(db_session, tenant) == 5
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None and renewal.amount_due == Decimal("99.00")


# ------------------------------------------------------- BILL-10 / BILL-20


async def test_a_manual_payment_restores_a_suspended_workspace(db_session: AsyncSession) -> None:
    """BILL-10: a bank transfer settles exactly as a card does. BILL-20: once."""
    from app.core.exceptions import ConflictError

    tenant, owner = await _workspace(db_session)
    await _buy(db_session, tenant, owner, Paymob(), "pro", now=T0, transaction=700_101_001)
    subscription = await _subscription(db_session, tenant)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=subscription.current_period_end + timedelta(minutes=1))
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()

    service = InvoiceService(db_session, tenant_id=tenant.id)
    await service.record_payment(
        invoice_id=renewal.id,
        amount=Decimal("99.00"),
        provider="bank_transfer",
        reference="bt-1",
        currency="EGP",
        expected_revision=renewal.revision,
    )
    assert renewal.status is InvoiceStatus.PAID
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert await _agents(db_session, tenant) == 5

    with pytest.raises(ConflictError):
        await service.record_payment(
            invoice_id=renewal.id,
            amount=Decimal("1.00"),
            provider="bank_transfer",
            reference="bt-2",
        )


async def test_voiding_an_overdue_renewal_needs_an_explicit_policy(
    db_session: AsyncSession,
) -> None:
    """BILL-10: a void never silently reactivates; `waive` is recorded as a waiver."""
    from app.core.exceptions import ValidationError
    from app.db.models.billing import BillingAdjustment, BillingAdjustmentKind

    tenant, owner = await _workspace(db_session)
    await _buy(db_session, tenant, owner, Paymob(), "pro", now=T0, transaction=700_102_001)
    subscription = await _subscription(db_session, tenant)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=subscription.current_period_end + timedelta(minutes=1))
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()
    service = InvoiceService(db_session, tenant_id=tenant.id)

    with pytest.raises(ValidationError):
        await service.void(renewal.id, reason="written off")

    await service.void(renewal.id, reason="goodwill waiver", subscription_policy="waive")
    assert renewal.status is InvoiceStatus.VOID
    assert subscription.status is SubscriptionStatus.ACTIVE
    waiver = await db_session.scalar(
        select(BillingAdjustment).where(BillingAdjustment.invoice_id == renewal.id)
    )
    assert waiver is not None and waiver.kind is BillingAdjustmentKind.INVOICE_WAIVER


# --------------------------------------------------------------- BILL-09


async def test_a_hosted_payment_whose_callback_was_lost_is_recovered_by_inquiry(
    db_session: AsyncSession,
) -> None:
    """BILL-09. Paymob took the money; Wasla never heard. Reconciliation settles it.

    Through the same `CheckoutService.apply` a callback uses, with no charge
    sent, a resolved incident recording the recovery, and the saved card
    recovered by Card Token Inquiry.
    """
    from app.db.models.payment_method import PaymentMethod
    from app.services.payment_reconciliation_service import PaymentReconciler

    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    provider = paymob.provider(api_key="an-inquiry-api-key")
    started = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).start(
        plan_code="pro", actor=owner, now=T0
    )
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    order = str(payment.provider_order_id)
    success = json.loads(_callback(payment, transaction=700_900_001)[0])["obj"]
    paymob.inquiry = success
    paymob.card_tokens = [
        {
            "type": "TOKEN",
            "obj": {
                "id": 555,
                "token": "recovered-card-token-" + uuid.uuid4().hex,
                "masked_pan": "xxxx-xxxx-xxxx-2346",
                "card_subtype": "MasterCard",
                "order_id": order,
                "email": owner.email,
            },
        }
    ]
    paymob.requests.clear()

    verdict = await PaymentReconciler(
        session=db_session, provider=provider, default_plan_code="starter", settings=_settings()
    ).reconcile_payment(payment.id, now=T0 + timedelta(hours=1))

    assert verdict == "settled"
    assert payment.status is PaymentStatus.SUCCEEDED
    assert await _agents(db_session, tenant) == 5
    assert paymob.pays() == [], "reconciliation never charges"
    incident = await db_session.scalar(
        select(BillingIncident).where(BillingIncident.payment_id == payment.id)
    )
    assert incident is not None
    assert incident.kind is BillingIncidentKind.RECOVERED_BY_RECONCILIATION
    cards = (
        await db_session.scalars(select(PaymentMethod).where(PaymentMethod.tenant_id == tenant.id))
    ).all()
    assert len(cards) == 1


# ----------------------------------------------------------------- B29


async def test_a_callback_resolves_an_unresolved_automatic_attempt(
    db_session: AsyncSession,
) -> None:
    """Mutation survivor B29: the *callback* path closes an unknown MIT outcome.

    Only the reconciler path was tested, so a callback that stopped moving the
    attempt to `settled` left the invoice blocked from every further charge
    and from suspension until reconciliation - for ever without an API key.
    """
    tenant, owner = await _workspace(db_session)
    await _buy(db_session, tenant, owner, Paymob(), "pro", now=T0, transaction=700_290_001)
    db_session.add(
        saved_card(
            tenant_id=tenant.id,
            provider="paymob",
            token=f"tok-{uuid.uuid4().hex}",
            provider_token_id="1",
            masked_pan="xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    subscription = await _subscription(db_session, tenant)
    renewal_at = subscription.current_period_end + timedelta(minutes=1)
    worker = BillingWorker(database=as_database(SessionHandle(db_session)), settings=_settings())
    await worker._advance_due(now=renewal_at)
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == tenant.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
    )
    assert renewal is not None
    paymob = Paymob()
    provider = paymob.provider()
    outcome = await RecurringService(
        db_session, tenant_id=tenant.id, provider=provider, payment_tokens=PROTECTOR
    ).collect(renewal, subscription=subscription, now=renewal_at)
    attempt = await db_session.get(Payment, outcome.payment_id)
    assert attempt is not None and attempt.is_unresolved_collection

    assert (
        await _apply(
            db_session,
            tenant.id,
            provider,
            _callback(attempt, transaction=700_290_002),
            now=renewal_at,
        )
        == APPLIED
    )

    assert attempt.collection_state is CollectionState.SETTLED
    assert renewal.status is InvoiceStatus.PAID
