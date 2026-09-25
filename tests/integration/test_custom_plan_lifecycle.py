"""A custom plan over its life: bought, re-versioned, migrated at renewal (spec 83).

Driven through the real services and the real billing sweep against
PostgreSQL. Paymob is faked at the socket only; the callback that pays for the
custom plan is signed and verified by the real adapter.

1. Tenant A's custom plan, all seven limits, is offered to A and bought by A's
   owner accepting the offer at checkout; A is held to its version 1.
2. Tenant B cannot see it, buy it or be put on it.
3. Version 2 is published and A stays on version 1.
4. A migration is scheduled; A's renewal is billed at version 2 and A moves to
   version 2 when - and only when - that renewal is paid, exactly once.
5. A top-up bought on the custom plan adds to it and survives the plan change
   untouched (spec 46).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import CustomPlanNotAvailableError, ValidationError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import BillingInterval, LimitKey, PlanScope, PlanVersion
from app.db.models.enums import PlatformRole
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.user import User
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.schemas.custom_plan import CustomPlanOfferCreate
from app.schemas.platform_billing import (
    ChangeMode,
    PlanCreate,
    PlanVersionCreate,
    SubscriptionChangePlan,
)
from app.services.checkout_service import APPLIED, CheckoutService
from app.services.custom_plan_offer_service import CustomPlanOfferService
from app.services.invoice_service import InvoiceService
from app.services.subscription_service import SubscriptionService
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.fakes import as_database
from tests.integration.topup_harness import (
    GIB,
    Paymob,
    base_now,
    buy,
    catalogue,
    later,
    pay,
    product,
    standing,
    workspace,
)

pytestmark = pytest.mark.integration

SEVEN = {
    "period_messages": 100_000,
    "period_ai_turns": 40_000,
    "period_campaign_messages": 50_000,
    "storage_bytes": 100 * GIB,
    "whatsapp_numbers": 5,
    "team_members": 30,
    "knowledge_documents": 3_000,
}


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", default_plan_code="starter")


class _Handle:
    """Hands the worker the suite's transaction-scoped session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


async def test_a_custom_plan_is_bought_reversioned_and_migrated_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = base_now()
    await catalogue(db_session)
    alpha, alpha_owner, subscription = await workspace(db_session, now=now, name="Alpha")
    beta, beta_owner, beta_subscription = await workspace(db_session, now=now, name="Beta")
    staff = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    db_session.add(staff)
    await db_session.flush()
    paymob = Paymob()
    code = f"alpha-custom-{uuid.uuid4().hex[:6]}"

    # 1. The custom plan, and Alpha's owner buys it at checkout.
    admin = PlanCatalogAdmin(db_session)
    created = await admin.create(
        PlanCreate(
            code=code,
            name="Alpha Enterprise",
            price=Decimal("1500.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 5, **SEVEN},
            scope=PlanScope.TENANT,
            tenant_id=alpha.id,
            is_public=False,
            reason="Negotiated terms.",
        ),
        actor=staff,
        now=now,
    )
    assert created.current_version is not None
    v1 = created.current_version.id
    offer = await PlatformCustomPlanOffers(db_session).offer(
        alpha.id,
        CustomPlanOfferCreate(plan_version_id=v1, reason="Negotiated terms."),
        actor=staff,
        now=now,
    )
    accepted = await CustomPlanOfferService(
        db_session,
        tenant_id=alpha.id,
        checkout=CheckoutService(db_session, tenant_id=alpha.id, provider=paymob.provider()),
    ).accept(offer.id, actor=alpha_owner, idempotency_key=None, now=now)
    started = accepted.checkout
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    assert (
        await pay(db_session, alpha.id, paymob, payment, transaction=920_000_001, now=now)
        == APPLIED
    )
    await db_session.refresh(subscription)
    assert subscription.plan_version_id == v1
    for key, value in SEVEN.items():
        assert (await standing(db_session, alpha, LimitKey(key), at=now)).base_limit == value

    # 2. Beta cannot buy or hold it.
    with pytest.raises(ValidationError):
        await CheckoutService(db_session, tenant_id=beta.id, provider=paymob.provider()).start(
            plan_code=code, actor=beta_owner, now=now
        )
    version = await db_session.get(PlanVersion, v1)
    assert version is not None
    with pytest.raises(CustomPlanNotAvailableError):
        await SubscriptionService(db_session, tenant_id=beta.id).apply_purchase(
            version=version, now=now
        )
    await db_session.refresh(beta_subscription)
    assert beta_subscription.plan_version_id != v1

    # 5 (first half). A top-up on top of the custom plan.
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_MESSAGES, quantity=50_000)
    top, top_payment = await buy(db_session, alpha, alpha_owner, paymob, item, now=now)
    await pay(db_session, alpha.id, paymob, top_payment, transaction=920_000_002, now=now)
    messages = await standing(db_session, alpha, LimitKey.PERIOD_MESSAGES, at=now)
    assert (messages.base_limit, messages.topup_limit, messages.limit) == (
        100_000,
        50_000,
        150_000,
    )

    # 3. Version 2, and Alpha stays on version 1.
    v2_read = await admin.create_version(
        created.id,
        PlanVersionCreate(
            price=Decimal("1800.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 5, **SEVEN, "period_ai_turns": 60_000},
            expected_version=1,
            reason="Year two.",
        ),
        actor=staff,
        now=now,
    )
    await db_session.refresh(subscription)
    assert subscription.plan_version_id == v1
    assert (
        await standing(db_session, alpha, LimitKey.PERIOD_AI_TURNS, at=now)
    ).base_limit == 40_000

    # 4. Migrate at renewal: billed at v2, adopted only once paid, exactly once.
    await PlatformBillingOperations(db_session, settings=_settings()).change_plan(
        subscription.id,
        SubscriptionChangePlan(
            plan_version_id=v2_read.id,
            mode=ChangeMode.NEXT_RENEWAL,
            reason="Year two begins at renewal.",
            expected_revision=subscription.revision,
        ),
        actor=staff,
        now=now,
    )
    boundary = subscription.current_period_end
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: None)
    worker = BillingWorker(database=as_database(_Handle(db_session)), settings=_settings())
    await worker.run_once(now=later(boundary, minutes=1))

    await db_session.refresh(subscription)
    renewal = await db_session.scalar(
        select(Invoice)
        .where(Invoice.tenant_id == alpha.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
        .where(Invoice.period_start == boundary)
    )
    assert renewal is not None
    assert (renewal.plan_version_id, renewal.amount_due) == (v2_read.id, Decimal("1800.00"))
    assert subscription.plan_version_id == v1, "not adopted before it is paid"

    await InvoiceService(db_session, tenant_id=alpha.id).record_payment(
        invoice_id=renewal.id,
        amount=Decimal("1800.00"),
        provider="bank_transfer",
        reference="ALPHA-Y2",
        now=later(boundary, hours=1),
        currency="EGP",
        actor=staff,
    )
    await db_session.refresh(subscription)
    await db_session.refresh(renewal)
    assert renewal.status is InvoiceStatus.PAID
    assert subscription.plan_version_id == v2_read.id
    assert subscription.scheduled_plan_version_id is None

    await worker.run_once(now=later(boundary, hours=2))
    renewals = await db_session.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.tenant_id == alpha.id)
        .where(Invoice.purpose == InvoicePurpose.RENEWAL)
        .where(Invoice.period_start == boundary)
    )
    assert renewals == 1
    adoptions = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.tenant_id == alpha.id)
        .where(AuditLog.action == AuditAction.SUBSCRIPTION_PLAN_CHANGED)
        .where(AuditLog.meta["reason"].astext == "renewal_settled")
    )
    assert adoptions == 1
    after = later(boundary, hours=3)
    assert (
        await standing(db_session, alpha, LimitKey.PERIOD_AI_TURNS, at=after)
    ).base_limit == 60_000

    # 5 (second half). The top-up ended with the period it was bought in; its
    # history is untouched and the new period's base is the migrated plan's.
    purchase = await db_session.get(TopupPurchase, top.purchase_id)
    assert purchase is not None
    await db_session.refresh(purchase)
    assert purchase.status is TopupStatus.EXPIRED
    assert (purchase.quantity, purchase.expires_at) == (50_000, boundary)
    messages = await standing(db_session, alpha, LimitKey.PERIOD_MESSAGES, at=after)
    assert (messages.topup_limit, messages.limit) == (0, 100_000)
    assert boundary - now > timedelta(days=27)
