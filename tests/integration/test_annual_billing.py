# ruff: noqa: F811 - the platform API harness fixtures are imported by name.
"""Monthly and yearly prices of one plan, end to end (ADR-116).

Driven through the real services, the real Paymob adapter (callbacks signed and
verified with its own HMAC), the real settlement engine and the real billing
sweep, against PostgreSQL. Paymob is faked at the socket only, so every amount
asserted here is the amount the adapter would put on the wire.

What is proved:

- **Catalogue.** One version, a monthly and a yearly price, identical limits.
  Prices are published, retired and read; never edited, never deleted; one
  active price per term. Retiring a price moves nobody; a new price reaches new
  customers only.
- **Checkout.** A customer buys an explicit price. A yearly purchase is one
  invoice for the full year and one hosted payment for that amount; it opens a
  twelve-month billing term and a one-month usage cycle.
- **Usage.** Allowances reset every calendar month inside the year - never
  twelve months at once - and the monthly roll bills nothing and charges
  nothing.
- **Renewal.** A yearly renewal is one invoice for the year; with a saved card
  one MOTO charge for the full annual amount; without one, no charge at all.
- **Changes.** Monthly to yearly is a purchase now; yearly to monthly, and every
  downgrade, waits for the end of the paid year at a pinned price; cancelling
  ends at the year's end.
- **Top-ups.** A usage top-up ends with its monthly cycle, a capacity top-up
  with the billing term; expiry deletes nothing.
- **Custom plans and offers**, priced monthly and yearly, offered at one exact
  price the customer cannot change; complimentary assignment moves no money.
- **Isolation.** Another workspace's custom price is refused by the services
  and by the database.

Each scenario ends by running the independent invariant ledger
(`annual_billing_oracle.py`) over its own workspaces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.exceptions import ConflictError, PaymentRequiredError, ValidationError
from app.core.security import TokenClaims, TokenType
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import PlatformRole, TenantRole
from app.db.models.invoice import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
)
from app.db.models.membership import Membership
from app.db.models.payment_method import PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.custom_plan_admin import CustomPlanAdmin
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.schemas.custom_plan import CustomPlanBasis, CustomPlanCreate, CustomPlanOfferCreate
from app.schemas.platform_billing import (
    ChangeMode,
    FinancialBasis,
    PlanPriceCreate,
    PlanPriceRetire,
    PlanVersionCreate,
    PriceSpec,
    SubscriptionChangePlan,
)
from app.services.checkout_service import APPLIED, DUPLICATE, CheckoutService, StartedCheckout
from app.services.custom_plan_offer_service import CustomPlanOfferService
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from app.services.topup_service import TopupService
from app.services.usage_service import UsageRecorder
from tests.billing_fixtures import add_owner
from tests.integration.annual_billing_oracle import ledger_violations, oracle_entitlement
from tests.integration.plan_catalogue import own_plan
from tests.integration.test_billing_remediation_journeys import (
    Paymob,
    _apply,
    _callback,
    _settings,
    _worker,
)
from tests.integration.test_platform_billing_api import app, desk, http  # noqa: F401
from tests.integration.topup_harness import GIB, product
from tests.payment_tokens import saved_card
from tests.paymob_orders import CARD_INTEGRATION_ID, MOTO_INTEGRATION_ID

pytestmark = pytest.mark.integration

# 1 October 2026, 09:00: the specification's worked example.
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

SEVEN_PRO: dict[str, int] = {
    "agents": 5,
    "period_messages": 10_000,
    "period_ai_turns": 5_000,
    "period_campaign_messages": 1_000,
    "storage_bytes": 10 * GIB,
    "whatsapp_numbers": 1,
    "team_members": 10,
    "knowledge_documents": 500,
}
SEVEN_BUSINESS: dict[str, int] = {
    "agents": 20,
    "period_messages": 100_000,
    "period_ai_turns": 25_000,
    "period_campaign_messages": 50_000,
    "storage_bytes": 100 * GIB,
    "whatsapp_numbers": 5,
    "team_members": 10,
    "knowledge_documents": 3_000,
}
# Clearly synthetic test prices - never a production price (spec 123).
PRO_MONTHLY, PRO_YEARLY = Decimal("99.00"), Decimal("990.00")
BUSINESS_MONTHLY, BUSINESS_YEARLY = Decimal("299.00"), Decimal("2990.00")


# ------------------------------------------------------------------ harness


async def _staff(session: AsyncSession) -> User:
    user = User(
        email=f"annual-staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def _catalogue(session: AsyncSession, staff: User) -> dict[str, PlanPrice]:
    """Starter (free), Pro and Business, each sold monthly and yearly."""
    await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"agents": 1, "period_ai_turns": 100, "whatsapp_numbers": 1},
    )
    await own_plan(session, code="pro", price=PRO_MONTHLY, limits=SEVEN_PRO)
    await own_plan(session, code="business", price=BUSINESS_MONTHLY, limits=SEVEN_BUSINESS)
    admin = PlanCatalogAdmin(session)
    catalog = PlanCatalog(session)
    prices: dict[str, PlanPrice] = {}
    for code, yearly in (("pro", PRO_YEARLY), ("business", BUSINESS_YEARLY)):
        plan = await session.scalar(select(Plan).where(Plan.code == code))
        assert plan is not None
        version = await catalog.current_version(plan, at=T0)
        assert version is not None
        monthly = await catalog.price_for_term(version, interval=BillingInterval.MONTHLY)
        assert monthly is not None
        existing = await catalog.price_for_term(version, interval=BillingInterval.YEARLY)
        if existing is None:
            created = await admin.create_price(
                version.id,
                PlanPriceCreate(
                    billing_interval=BillingInterval.YEARLY,
                    amount=yearly,
                    reason="Synthetic yearly test price.",
                ),
                actor=staff,
                now=T0 - timedelta(days=1),
            )
            existing = await catalog.get_price(created.id)
        assert existing is not None and existing.amount == yearly
        prices[f"{code}_monthly"] = monthly
        prices[f"{code}_yearly"] = existing
    return prices


async def _workspace(
    session: AsyncSession, *, now: datetime = T0, name: str = "Annual Co"
) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, slug=f"annual-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await add_owner(session, tenant)
    await SubscriptionService(session, tenant_id=tenant.id).start(
        plan_code="starter", now=now, self_service=False
    )
    return tenant, owner


async def _open(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    paymob: Paymob,
    price: PlanPrice,
    *,
    now: datetime,
) -> tuple[StartedCheckout, Invoice, Payment]:
    started = await CheckoutService(session, tenant_id=tenant.id, provider=paymob.provider()).start(
        plan_price_id=price.id, actor=owner, now=now
    )
    invoice = await session.get(Invoice, started.invoice_id)
    payment = await session.get(Payment, started.payment_id)
    assert invoice is not None and payment is not None
    return started, invoice, payment


async def _buy(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    paymob: Paymob,
    price: PlanPrice,
    *,
    now: datetime,
    transaction: int,
) -> tuple[Invoice, Payment]:
    _, invoice, payment = await _open(session, tenant, owner, paymob, price, now=now)
    signed = _callback(payment, transaction=transaction)
    assert await _apply(session, tenant.id, paymob.provider(), signed, now=now) == APPLIED
    await session.refresh(invoice)
    return invoice, payment


async def _subscription(session: AsyncSession, tenant: Tenant) -> Subscription:
    row = await session.scalar(select(Subscription).where(Subscription.tenant_id == tenant.id))
    assert row is not None
    await session.refresh(row)
    return row


def _card(tenant: Tenant) -> Any:
    return saved_card(
        tenant_id=tenant.id,
        provider="paymob",
        token=f"tok-{uuid.uuid4().hex}",
        provider_token_id="1",
        masked_pan="xxxx-2346",
        brand="MasterCard",
        status=PaymentMethodStatus.ACTIVE,
        is_default=True,
    )


async def _entitlements(session: AsyncSession, tenant: Tenant, at: datetime) -> EntitlementService:
    return EntitlementService(
        session, tenant_id=tenant.id, default_plan_code="starter", clock=lambda: at
    )


async def _spend(session: AsyncSession, tenant: Tenant, turns: int, *, at: datetime) -> None:
    """Record `turns` AI turns as having happened at `at`."""
    recorder = UsageRecorder(session, tenant_id=tenant.id)
    event = recorder.record(UsageEventType.AI_TURN, quantity=turns)
    assert event is not None
    event.occurred_at = at
    await session.flush()


async def _invoices(session: AsyncSession, tenant: Tenant) -> list[Invoice]:
    return list(
        (
            await session.scalars(
                select(Invoice)
                .where(Invoice.tenant_id == tenant.id)
                .order_by(Invoice.created_at, Invoice.period_start)
            )
        ).all()
    )


async def _clean(session: AsyncSession, *tenants: Tenant) -> None:
    """The invariant ledger, over these workspaces' rows."""
    found = await ledger_violations(session, tenant_ids=[tenant.id for tenant in tenants])
    assert found == {}, f"ledger violations: {found}"


def _year(moment: datetime) -> datetime:
    return moment.replace(year=moment.year + 1)


def _login(app: FastAPI, user: User, *, tenant_id: uuid.UUID | None = None) -> None:
    """Act as `user`, in `tenant_id`'s workspace when one is named."""
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=tenant_id,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=user, claims=claims)


async def _verified_owner(session: AsyncSession, tenant: Tenant) -> User:
    """The workspace's owner, with a verified address as the routes require."""
    owner = await session.scalar(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == tenant.id)
        .where(Membership.role == TenantRole.TENANT_OWNER)
    )
    assert owner is not None
    owner.email_verified_at = owner.email_verified_at or datetime.now(UTC)
    await session.flush()
    return owner


# ============================================================ platform prices


async def test_one_version_is_sold_monthly_and_yearly_without_copying_it(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    business = await db_session.scalar(select(Plan).where(Plan.code == "business"))
    assert business is not None
    versions = await PlanCatalogAdmin(db_session).versions(business.id)
    current = versions[0]
    assert len({price.plan_version_id for price in prices.values()}) == 2
    assert prices["business_yearly"].plan_version_id == prices["business_monthly"].plan_version_id
    assert prices["business_yearly"].plan_version_id == current.id, "no copied version"
    shown = {(price.billing_interval, price.amount) for price in current.prices}
    assert (BillingInterval.MONTHLY, "299.00") in shown
    assert (BillingInterval.YEARLY, "2990.00") in shown
    audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_PLAN_PRICE_CREATED)
        .where(AuditLog.target_id == prices["business_yearly"].id)
    )
    assert audit is not None and audit.meta is not None
    assert audit.meta["billing_interval"] == "yearly"
    assert audit.meta["amount"] == "2990.00"
    assert audit.meta["plan_code"] == "business"


async def test_a_price_is_refused_for_a_duplicate_term_a_free_version_or_a_retired_plan(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    admin = PlanCatalogAdmin(db_session)
    catalog = PlanCatalog(db_session)
    yearly = PlanPriceCreate(
        billing_interval=BillingInterval.YEARLY, amount=Decimal("3000.00"), reason="Again."
    )
    with pytest.raises(ConflictError):
        await admin.create_price(
            prices["business_yearly"].plan_version_id, yearly, actor=staff, now=T0
        )

    starter = await db_session.scalar(select(Plan).where(Plan.code == "starter"))
    assert starter is not None
    free = await catalog.current_version(starter, at=T0)
    assert free is not None and await catalog.prices(free) == []
    with pytest.raises(ValidationError):
        await admin.create_price(free.id, yearly, actor=staff, now=T0)

    # A retired plan is sold to nobody new, so it is given no new price - even
    # for a term whose previous price was retired first.
    await admin.retire_price(
        prices["pro_yearly"].id, PlanPriceRetire(reason="Plan closing."), actor=staff, now=T0
    )
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    pro.is_active = False
    await db_session.flush()
    with pytest.raises(ValidationError, match="retired plan"):
        await admin.create_price(
            prices["pro_yearly"].plan_version_id,
            PlanPriceCreate(
                billing_interval=BillingInterval.YEARLY, amount=Decimal("1090.00"), reason="Late."
            ),
            actor=staff,
            now=T0,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"billing_interval": "yearly", "amount": "-1.00", "reason": "Negative."},
        {"billing_interval": "yearly", "amount": "0.00", "reason": "Zero."},
        {"billing_interval": "fortnightly", "amount": "10.00", "reason": "Unknown."},
        {"billing_interval": "yearly", "interval_count": 0, "amount": "10.00", "reason": "Zero."},
        {"billing_interval": "yearly", "interval_count": 2, "amount": "10.00", "reason": "Two."},
        {"billing_interval": "yearly", "amount": "10.00", "currency": "USD", "reason": "USD."},
    ],
)
def test_a_price_request_is_validated_where_it_enters(payload: dict[str, Any]) -> None:
    from pydantic import ValidationError as SchemaError

    with pytest.raises(SchemaError):
        PlanPriceCreate.model_validate(payload)


def test_month_and_year_are_accepted_as_the_stored_spellings() -> None:
    parsed = PlanPriceCreate.model_validate(
        {"billing_interval": "year", "amount": "2990.00", "reason": "Spelled out."}
    )
    assert parsed.billing_interval is BillingInterval.YEARLY
    assert PriceSpec.model_validate({"interval": "month", "amount": "1"}).billing_interval is (
        BillingInterval.MONTHLY
    )


async def test_a_superseded_version_cannot_be_given_a_new_price(db_session: AsyncSession) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    admin = PlanCatalogAdmin(db_session)
    latest = (await admin.versions(pro.id))[0]
    await admin.create_version(
        pro.id,
        PlanVersionCreate(
            prices=[PriceSpec(billing_interval=BillingInterval.MONTHLY, amount=Decimal("109"))],
            limits=SEVEN_PRO,
            expected_version=latest.version,
            reason="Pro v-next.",
        ),
        actor=staff,
        now=T0,
    )
    with pytest.raises(ValidationError):
        await admin.create_price(
            prices["pro_yearly"].plan_version_id,
            PlanPriceCreate(
                billing_interval=BillingInterval.YEARLY, amount=Decimal("1090"), reason="Late."
            ),
            actor=staff,
            now=T0,
        )


async def test_a_version_is_published_with_every_price_it_is_sold_at(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    await _catalogue(db_session, staff)
    admin = PlanCatalogAdmin(db_session)
    both = await admin.create(
        _plan_create(
            "both-" + uuid.uuid4().hex[:6],
            prices=[
                PriceSpec(billing_interval=BillingInterval.MONTHLY, amount=Decimal("50")),
                PriceSpec(billing_interval=BillingInterval.YEARLY, amount=Decimal("500")),
            ],
        ),
        actor=staff,
        now=T0,
    )
    yearly_only = await admin.create(
        _plan_create(
            "yearly-" + uuid.uuid4().hex[:6],
            prices=[PriceSpec(billing_interval=BillingInterval.YEARLY, amount=Decimal("700"))],
        ),
        actor=staff,
        now=T0,
    )
    free = await admin.create(
        _plan_create("free-" + uuid.uuid4().hex[:6], prices=[]), actor=staff, now=T0
    )
    # `latest_version`: T0 lies ahead of the real clock the read uses.
    assert both.latest_version is not None
    assert sorted((p.billing_interval.value, p.amount) for p in both.latest_version.prices) == [
        ("monthly", "50.00"),
        ("yearly", "500.00"),
    ]
    assert (both.latest_version.price, both.latest_version.interval) == (
        "50.00",
        BillingInterval.MONTHLY,
    ), "the version row records the monthly price as published"
    assert yearly_only.latest_version is not None
    assert [(p.billing_interval, p.amount) for p in yearly_only.latest_version.prices] == [
        (BillingInterval.YEARLY, "700.00")
    ]
    assert free.latest_version is not None
    assert free.latest_version.prices == [] and not free.latest_version.billing_required


def _plan_create(code: str, *, prices: list[PriceSpec]) -> Any:
    from app.schemas.platform_billing import PlanCreate

    return PlanCreate(
        code=code, name=code.title(), prices=prices, limits=SEVEN_PRO, reason="Test plan."
    )


async def test_retiring_a_price_keeps_its_subscribers_and_its_history(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2,990 a year is retired and 3,290 published: the subscriber renews at 2,990."""
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, prices["business_yearly"], now=T0, transaction=1)
    admin = PlanCatalogAdmin(db_session)
    retired = await admin.retire_price(
        prices["business_yearly"].id,
        PlanPriceRetire(reason="Annual price rises."),
        actor=staff,
        now=T0 + timedelta(days=10),
    )
    assert not retired.active and retired.retired_at is not None
    assert retired.references is not None and retired.references.subscriptions == 1
    with pytest.raises(ConflictError):
        await admin.retire_price(
            prices["business_yearly"].id, PlanPriceRetire(reason="Twice."), actor=staff
        )
    raised = await admin.create_price(
        prices["business_yearly"].plan_version_id,
        PlanPriceCreate(
            billing_interval=BillingInterval.YEARLY,
            amount=Decimal("3290.00"),
            reason="New annual price.",
        ),
        actor=staff,
        now=T0 + timedelta(days=10),
    )
    history = await admin.prices(prices["business_yearly"].plan_version_id)
    yearly_history = sorted(
        (p.amount, p.active) for p in history if p.billing_interval is BillingInterval.YEARLY
    )
    assert yearly_history == [("2990.00", False), ("3290.00", True)]

    # A new customer sees and buys 3,290; the old id is refused.
    catalog = PlanCatalog(db_session)
    business = await db_session.scalar(select(Plan).where(Plan.code == "business"))
    assert business is not None
    version = await catalog.current_version(business)
    assert version is not None
    offered = {(p.billing_interval, p.amount) for p in await catalog.prices(version)}
    assert offered == {
        (BillingInterval.MONTHLY, BUSINESS_MONTHLY),
        (BillingInterval.YEARLY, Decimal("3290.00")),
    }
    newcomer, newcomer_owner = await _workspace(db_session, name="Newcomer")
    with pytest.raises(ValidationError):
        await _open(db_session, newcomer, newcomer_owner, paymob, prices["business_yearly"], now=T0)

    # The existing subscriber's renewal a year on is 2,990, the price it holds.
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == prices["business_yearly"].id
    worker = _worker(db_session, monkeypatch, paymob.provider())
    await worker.run_once(now=subscription.current_period_end + timedelta(minutes=1))
    renewal = next(i for i in await _invoices(db_session, tenant) if i.purpose.value == "renewal")
    assert (renewal.amount_due, renewal.plan_price_id) == (
        BUSINESS_YEARLY,
        prices["business_yearly"].id,
    )
    assert raised.amount == "3290.00"
    await _clean(db_session, tenant, newcomer)


async def test_a_price_is_never_edited_the_database_refuses_it(db_session: AsyncSession) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    price_id = prices["business_yearly"].id
    for statement in (
        "UPDATE plan_prices SET amount = 3290 WHERE id = :id",
        "UPDATE plan_prices SET billing_interval = 'monthly' WHERE id = :id",
        "UPDATE plan_prices SET plan_version_id = gen_random_uuid() WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="immutable|violates"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement), {"id": price_id})
    await db_session.execute(
        text(
            "UPDATE plan_prices SET retired_at = created_at + interval '1 day',"
            " retirement_reason = 'r' WHERE id = :id"
        ),
        {"id": price_id},
    )
    with pytest.raises(DBAPIError, match="stays retired"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE plan_prices SET retired_at = NULL WHERE id = :id"), {"id": price_id}
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO plan_prices (id, plan_version_id, billing_interval,"
                    " interval_count, amount, currency, created_at) VALUES"
                    " (gen_random_uuid(), :v, 'monthly', 1, 5, 'EGP', now())"
                ),
                {"v": prices["business_monthly"].plan_version_id},
            )


async def test_the_platform_price_api_refuses_edits_and_tenants(
    app: FastAPI, http: AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, _ = await _workspace(db_session)
    owner = await _verified_owner(db_session, tenant)
    version_id = prices["business_yearly"].plan_version_id
    base = "/api/v1/platform/billing"

    _login(app, staff)
    listed = await http.get(f"{base}/plan-versions/{version_id}/prices")
    assert listed.status_code == 200, listed.text
    assert {p["billing_interval"] for p in listed.json()} == {"monthly", "yearly"}
    version = await http.get(f"{base}/plan-versions/{version_id}")
    assert version.status_code == 200 and len(version.json()["prices"]) == 2
    patched = await http.patch(
        f"{base}/prices/{prices['business_yearly'].id}", json={"amount": "3290.00"}
    )
    assert patched.status_code == 409, patched.text
    assert "retire" in patched.text.lower()
    duplicate = await http.post(
        f"{base}/plan-versions/{version_id}/prices",
        json={"billing_interval": "year", "amount": "2990.00", "reason": "Duplicate."},
    )
    assert duplicate.status_code == 409, duplicate.text
    quarterly = await http.post(
        f"{base}/plan-versions/{version_id}/prices",
        json={
            "billing_interval": "yearly",
            "interval_count": 3,
            "amount": "1.00",
            "reason": "Quarterly.",
        },
    )
    assert quarterly.status_code == 422

    _login(app, owner, tenant_id=tenant.id)
    for method, url, body in (
        (
            "post",
            f"{base}/plan-versions/{version_id}/prices",
            {"billing_interval": "yearly", "amount": "1.00", "reason": "Tenant tries."},
        ),
        ("post", f"{base}/prices/{prices['business_yearly'].id}/retire", {"reason": "Tries."}),
        ("get", f"{base}/plan-versions/{version_id}/prices", None),
        ("get", f"{base}/prices/{prices['business_yearly'].id}", None),
    ):
        response = await http.request(method, url, json=body)
        assert response.status_code == 403, (url, response.text)


async def test_the_catalogue_offers_monthly_and_yearly_and_starter_is_free(
    app: FastAPI, http: AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, _ = await _workspace(db_session)
    owner = await _verified_owner(db_session, tenant)
    _login(app, owner, tenant_id=tenant.id)
    response = await http.get("/api/v1/billing/plans")
    assert response.status_code == 200, response.text
    plans = {plan["code"]: plan for plan in response.json()}
    business = plans["business"]
    assert {(p["interval"], p["amount"]) for p in business["prices"]} == {
        ("monthly", "299.00"),
        ("yearly", "2990.00"),
    }
    assert business["billing_required"] is True
    # Earlier clients still read the monthly price in `price`.
    assert (business["price"], business["interval"]) == ("299.00", "monthly")
    assert {p["id"] for p in business["prices"]} == {
        str(prices["business_monthly"].id),
        str(prices["business_yearly"].id),
    }
    assert plans["starter"]["billing_required"] is False
    assert plans["starter"]["prices"] == []

    # The summary shows the paid term and the usage cycle apart.
    await _buy(
        db_session,
        tenant,
        owner,
        Paymob(),
        prices["business_yearly"],
        now=T0,
        transaction=10_900_001,
    )
    summary = await http.get("/api/v1/billing/summary")
    assert summary.status_code == 200, summary.text
    held = summary.json()["subscription"]
    assert held["billing_interval"] == "yearly"
    assert held["plan_price"]["amount"] == "2990.00"
    assert held["paid_through"] == held["billing_period_end"]
    assert held["next_renewal_amount"] == "2990.00"
    assert held["usage_period_end"] < held["billing_period_end"]


async def test_a_yearly_checkout_charges_one_year_and_opens_a_year_with_a_month_of_usage(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    started, invoice, payment = await _open(
        db_session, tenant, owner, paymob, prices["business_yearly"], now=T0
    )
    assert (started.amount, invoice.amount_due, payment.amount) == (BUSINESS_YEARLY,) * 3
    assert (invoice.billing_interval, invoice.interval_count) == (BillingInterval.YEARLY, 1)
    assert invoice.plan_price_id == prices["business_yearly"].id
    assert invoice.period_end == _year(invoice.period_start), "provisionally one year"
    intention = paymob.intentions()[-1]["body"]
    assert intention["amount"] == 299_000, "the full annual amount, in piastres"
    assert intention["payment_methods"] == [CARD_INTEGRATION_ID], "hosted card, never MOTO"

    paid_at = T0 + timedelta(minutes=4)
    signed = _callback(payment, transaction=11_000_001)
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=paid_at) == APPLIED
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == prices["business_yearly"].id
    assert (subscription.current_period_start, subscription.current_period_end) == (
        paid_at,
        _year(paid_at),
    )
    assert (subscription.usage_period_start, subscription.usage_period_end) == (
        paid_at,
        paid_at.replace(month=11),
    )
    await db_session.refresh(invoice)
    assert (invoice.period_start, invoice.period_end) == (paid_at, _year(paid_at))
    # The replay changes nothing.
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=paid_at) == DUPLICATE
    again = await _subscription(db_session, tenant)
    assert again.current_period_end == _year(paid_at)
    assert len(await _invoices(db_session, tenant)) == 1, "one invoice for the year, not twelve"
    await _clean(db_session, tenant)


async def test_a_monthly_checkout_is_unchanged_one_month_one_cycle(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    invoice, _ = await _buy(
        db_session, tenant, owner, paymob, prices["pro_monthly"], now=T0, transaction=11_100_001
    )
    subscription = await _subscription(db_session, tenant)
    assert (invoice.amount_due, invoice.billing_interval) == (PRO_MONTHLY, BillingInterval.MONTHLY)
    assert subscription.current_period_end == T0.replace(month=11)
    assert (subscription.usage_period_start, subscription.usage_period_end) == (
        subscription.current_period_start,
        subscription.current_period_end,
    ), "a monthly term is its own usage cycle"
    # A plan code alone still buys the monthly price.
    other, other_owner = await _workspace(db_session, name="Legacy client")
    legacy = await CheckoutService(
        db_session, tenant_id=other.id, provider=paymob.provider()
    ).start(plan_code="business", actor=other_owner, now=T0)
    assert legacy.amount == BUSINESS_MONTHLY
    await _clean(db_session, tenant, other)


async def test_a_checkout_never_trusts_the_client_for_price_term_or_amount(
    app: FastAPI, http: AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, _ = await _workspace(db_session)
    _login(app, await _verified_owner(db_session, tenant), tenant_id=tenant.id)
    for extra in (
        {"amount": "1.00"},
        {"currency": "USD"},
        {"billing_interval": "monthly"},
        {"interval": "month"},
        {"price": "1.00"},
        {"plan_code": "business"},
    ):
        response = await http.post(
            "/api/v1/billing/checkout",
            json={"plan_price_id": str(prices["business_yearly"].id), **extra},
        )
        assert response.status_code == 422, (extra, response.text)
    yearly = await http.post(
        "/api/v1/billing/checkout", json={"plan_price_id": str(prices["business_yearly"].id)}
    )
    assert yearly.status_code == 201, yearly.text
    assert yearly.json()["amount"] == "2990.00"


async def test_a_retired_price_and_a_superseded_version_are_refused_at_checkout(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await PlanCatalogAdmin(db_session).retire_price(
        prices["pro_yearly"].id, PlanPriceRetire(reason="Withdrawn."), actor=staff, now=T0
    )
    with pytest.raises(ValidationError, match="no longer offered"):
        await _open(db_session, tenant, owner, paymob, prices["pro_yearly"], now=T0)
    with pytest.raises(ValidationError, match="No such price"):
        await CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider()).start(
            plan_price_id=uuid.uuid4(), actor=owner, now=T0
        )


# ============================================================ usage cycles


async def test_annual_usage_resets_monthly_and_is_never_granted_twelve_times(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["business_yearly"], now=T0, transaction=12_000_1
    )
    subscription = await _subscription(db_session, tenant)
    first_cycle = (subscription.usage_period_start, subscription.usage_period_end)
    term = (subscription.current_period_start, subscription.current_period_end)
    limit = SEVEN_BUSINESS["period_ai_turns"]

    service = await _entitlements(db_session, tenant, T0 + timedelta(days=1))
    first = await service.check(LimitKey.PERIOD_AI_TURNS, additional=0)
    assert first.limit == limit, "a monthly allowance, not twelve months' worth"
    assert (first.period_start, first.period_end) == (
        subscription.usage_period_start,
        subscription.usage_period_end,
    )
    await _spend(db_session, tenant, limit, at=T0 + timedelta(days=2))
    exhausted = await (await _entitlements(db_session, tenant, T0 + timedelta(days=3))).check(
        LimitKey.PERIOD_AI_TURNS
    )
    assert not exhausted.allowed, "the month's allowance is spent"

    invoices_before = len(await _invoices(db_session, tenant))
    requests_before = len(paymob.requests)
    worker = _worker(db_session, monkeypatch, paymob.provider())
    november = first_cycle[1] + timedelta(minutes=5)
    # Before the sweep, the clock already resets the allowance.
    early = await (await _entitlements(db_session, tenant, november)).check(
        LimitKey.PERIOD_AI_TURNS
    )
    assert early.allowed and early.used == 0
    await worker.run_once(now=november)
    rolled = await _subscription(db_session, tenant)
    assert rolled.usage_period_start == first_cycle[1]
    assert rolled.usage_period_end == first_cycle[1].replace(month=12)
    assert (rolled.current_period_start, rolled.current_period_end) == term, "the term stays"
    assert len(await _invoices(db_session, tenant)) == invoices_before, "no invoice"
    assert len(paymob.requests) == requests_before, "no provider call"
    fresh = await (await _entitlements(db_session, tenant, november)).check(
        LimitKey.PERIOD_AI_TURNS, additional=0
    )
    assert (fresh.used, fresh.limit) == (0, limit)
    oracle = await oracle_entitlement(db_session, tenant.id, LimitKey.PERIOD_AI_TURNS, at=november)
    assert (oracle.limit, oracle.used, oracle.window) == (
        fresh.limit,
        fresh.used,
        (fresh.period_start, fresh.period_end),
    )
    await _clean(db_session, tenant)


async def test_a_worker_down_for_months_catches_up_in_one_pass_without_money(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["pro_yearly"], now=T0, transaction=12_100_001
    )
    requests_before = len(paymob.requests)
    april = datetime(2027, 4, 20, 12, tzinfo=UTC)
    handled = await _worker(db_session, monkeypatch, paymob.provider()).run_once(now=april)
    subscription = await _subscription(db_session, tenant)
    assert (subscription.usage_period_start, subscription.usage_period_end) == (
        datetime(2027, 4, 1, 9, 0, tzinfo=UTC),
        datetime(2027, 5, 1, 9, 0, tzinfo=UTC),
    )
    assert handled >= 1
    assert len(paymob.requests) == requests_before
    assert [i.purpose for i in await _invoices(db_session, tenant)] == [InvoicePurpose.CHECKOUT]
    await _clean(db_session, tenant)


async def test_monthly_and_yearly_subscribers_of_one_version_get_identical_limits(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    paymob = Paymob()
    monthly, monthly_owner = await _workspace(db_session, name="Monthly")
    yearly, yearly_owner = await _workspace(db_session, name="Yearly")
    await _buy(
        db_session,
        monthly,
        monthly_owner,
        paymob,
        prices["business_monthly"],
        now=T0,
        transaction=12_200_001,
    )
    await _buy(
        db_session,
        yearly,
        yearly_owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=12_200_002,
    )
    at = T0 + timedelta(days=3)
    for key in LimitKey:
        if key is LimitKey.OWNED_WORKSPACES:
            continue
        a = await (await _entitlements(db_session, monthly, at)).check(key, additional=0)
        b = await (await _entitlements(db_session, yearly, at)).check(key, additional=0)
        assert (a.base_limit, a.limit) == (b.base_limit, b.limit), key
        if key.value.startswith("period_"):
            assert (a.period_start, a.period_end) == (b.period_start, b.period_end), key
    await _clean(db_session, monthly, yearly)


# ============================================================ renewals


async def test_an_annual_renewal_with_a_saved_card_is_one_moto_charge_for_the_year(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=13_000_001,
    )
    db_session.add(_card(tenant))
    await db_session.flush()
    subscription = await _subscription(db_session, tenant)
    boundary = subscription.current_period_end
    provider = paymob.provider()
    worker = _worker(db_session, monkeypatch, provider)
    # Eleven monthly cycles roll without a single provider call.
    cycles = []
    for _ in range(11):
        await worker.run_once(now=subscription.usage_period_end + timedelta(minutes=1))
        subscription = await _subscription(db_session, tenant)
        cycles.append(subscription.usage_period_start)
    assert len(set(cycles)) == 11
    assert subscription.usage_period_end == boundary, "the twelfth cycle ends with the year"
    assert paymob.pays() == [], "no MOTO charge inside the paid year"
    assert [i.purpose for i in await _invoices(db_session, tenant)] == [InvoicePurpose.CHECKOUT]

    moment = boundary + timedelta(minutes=5)
    await worker.run_once(now=moment)
    charges = (
        await db_session.scalars(
            select(Payment)
            .where(Payment.tenant_id == tenant.id)
            .where(Payment.is_automatic.is_(True))
        )
    ).all()
    assert len(charges) == 1 and len(paymob.pays()) == 1
    charge = charges[0]
    assert charge.amount == BUSINESS_YEARLY
    pay_intention = paymob.intentions()[-1]["body"]
    assert pay_intention["amount"] == 299_000
    assert pay_intention["payment_methods"] == [MOTO_INTEGRATION_ID], "the MOTO integration"
    signed = _callback(charge, transaction=13_000_101)
    assert await _apply(db_session, tenant.id, provider, signed, now=moment) == APPLIED
    assert await _apply(db_session, tenant.id, provider, signed, now=moment) == DUPLICATE
    subscription = await _subscription(db_session, tenant)
    assert (subscription.current_period_start, subscription.current_period_end) == (
        boundary,
        _year(boundary),
    )
    assert subscription.usage_period_end == boundary.replace(month=11), "a month of usage"
    renewal = next(i for i in await _invoices(db_session, tenant) if i.purpose.value == "renewal")
    assert (renewal.amount_due, renewal.status, renewal.billing_interval) == (
        BUSINESS_YEARLY,
        InvoiceStatus.PAID,
        BillingInterval.YEARLY,
    )
    assert (renewal.period_start, renewal.period_end) == (boundary, _year(boundary))
    await _clean(db_session, tenant)


async def test_an_annual_renewal_without_a_card_is_never_charged_automatically(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["pro_yearly"], now=T0, transaction=13_100_001
    )
    subscription = await _subscription(db_session, tenant)
    boundary = subscription.current_period_end
    await _worker(db_session, monkeypatch, paymob.provider()).run_once(
        now=boundary + timedelta(minutes=5)
    )
    assert paymob.pays() == [], "0 automatic MOTO attempts"
    renewal = next(i for i in await _invoices(db_session, tenant) if i.purpose.value == "renewal")
    assert (renewal.status, renewal.amount_due, renewal.billing_interval) == (
        InvoiceStatus.OPEN,
        PRO_YEARLY,
        BillingInterval.YEARLY,
    )
    # The owner pays it at a hosted checkout, for the full year.
    started = await CheckoutService(
        db_session, tenant_id=tenant.id, provider=paymob.provider()
    ).start(invoice_id=renewal.id, actor=owner, now=boundary + timedelta(hours=1))
    assert started.amount == PRO_YEARLY
    assert paymob.intentions()[-1]["body"]["payment_methods"] == [CARD_INTEGRATION_ID]
    await _clean(db_session, tenant)


# ============================================================ changes of term


async def test_monthly_to_yearly_is_a_purchase_that_starts_a_new_year_at_settlement(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["pro_monthly"], now=T0, transaction=14_000_001
    )
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    with pytest.raises(PaymentRequiredError):
        await service.request_plan(plan_price_id=prices["pro_yearly"].id, actor=owner, now=T0)
    switch_at = T0 + timedelta(days=12)
    invoice, _ = await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["pro_yearly"],
        now=switch_at,
        transaction=14_000_002,
    )
    assert invoice.status is InvoiceStatus.PAID, "not refused as a duplicate of the month"
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == prices["pro_yearly"].id
    assert (subscription.current_period_start, subscription.current_period_end) == (
        switch_at,
        _year(switch_at),
    ), "a new annual term from settlement; no credit for the unused month"
    assert subscription.usage_period_start == switch_at
    await _clean(db_session, tenant)


async def test_yearly_to_monthly_waits_for_the_end_of_the_paid_year(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=14_100_001,
    )
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    with pytest.raises(ConflictError):
        await _open(db_session, tenant, owner, paymob, prices["business_monthly"], now=T0)
    await service.request_plan(
        plan_price_id=prices["business_monthly"].id, actor=owner, now=T0 + timedelta(days=30)
    )
    subscription = await _subscription(db_session, tenant)
    assert subscription.scheduled_plan_price_id == prices["business_monthly"].id
    assert subscription.scheduled_change_source is ScheduledChangeSource.DOWNGRADE
    assert subscription.plan_price_id == prices["business_yearly"].id, "the paid year stays"
    boundary = subscription.current_period_end
    worker = _worker(db_session, monkeypatch, paymob.provider())
    await worker.run_once(now=T0 + timedelta(days=60))
    assert (await _subscription(db_session, tenant)).plan_price_id == prices["business_yearly"].id
    await worker.run_once(now=boundary + timedelta(minutes=5))
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == prices["business_monthly"].id
    assert subscription.current_period_end == boundary.replace(month=boundary.month + 1)
    renewal = next(i for i in await _invoices(db_session, tenant) if i.purpose.value == "renewal")
    assert (renewal.amount_due, renewal.billing_interval) == (
        BUSINESS_MONTHLY,
        BillingInterval.MONTHLY,
    )
    await _clean(db_session, tenant)


@pytest.mark.parametrize(
    ("held", "target", "timing"),
    [
        ("pro_monthly", "pro_yearly", "purchase"),
        ("pro_monthly", "business_yearly", "purchase"),
        ("pro_yearly", "business_yearly", "purchase"),
        ("business_yearly", "pro_yearly", "scheduled"),
        ("business_yearly", "business_monthly", "scheduled"),
        ("business_yearly", "pro_monthly", "scheduled"),
    ],
)
async def test_the_upgrade_and_downgrade_matrix_is_enforced_by_the_services(
    db_session: AsyncSession, held: str, target: str, timing: str
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(db_session, tenant, owner, paymob, prices[held], now=T0, transaction=14_200_001)
    later = T0 + timedelta(days=20)
    if timing == "purchase":
        invoice, _ = await _buy(
            db_session, tenant, owner, paymob, prices[target], now=later, transaction=14_200_002
        )
        subscription = await _subscription(db_session, tenant)
        assert subscription.plan_price_id == prices[target].id
        assert subscription.current_period_start == later, "a full new term from settlement"
        assert invoice.amount_due == prices[target].amount, "the full new amount, no credit"
    else:
        with pytest.raises(ConflictError):
            await _open(db_session, tenant, owner, paymob, prices[target], now=later)
        await SubscriptionService(db_session, tenant_id=tenant.id).request_plan(
            plan_price_id=prices[target].id, actor=owner, now=later
        )
        subscription = await _subscription(db_session, tenant)
        assert subscription.plan_price_id == prices[held].id
        assert (subscription.scheduled_plan_version_id, subscription.scheduled_plan_price_id) == (
            prices[target].plan_version_id,
            prices[target].id,
        ), "the future price is pinned, not just the version"
    await _clean(db_session, tenant)


async def test_cancelling_an_annual_subscription_ends_with_the_year_not_the_month(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=15_000_001,
    )
    await SubscriptionService(db_session, tenant_id=tenant.id).cancel(
        now=T0 + timedelta(days=3), actor=owner
    )
    subscription = await _subscription(db_session, tenant)
    boundary = subscription.current_period_end
    worker = _worker(db_session, monkeypatch, paymob.provider())
    february = datetime(2027, 2, 10, tzinfo=UTC)
    await worker.run_once(now=february)
    subscription = await _subscription(db_session, tenant)
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.usage_period_start == datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
    agents = await (await _entitlements(db_session, tenant, february)).check(LimitKey.AGENTS)
    assert agents.limit == SEVEN_BUSINESS["agents"], "the paid year's entitlements remain"
    await worker.run_once(now=boundary + timedelta(minutes=1))
    subscription = await _subscription(db_session, tenant)
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert [i.purpose for i in await _invoices(db_session, tenant)] == [InvoicePurpose.CHECKOUT]
    await _clean(db_session, tenant)


async def test_a_scheduled_price_retired_before_the_boundary_is_still_honoured(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=15_100_001,
    )
    await SubscriptionService(db_session, tenant_id=tenant.id).request_plan(
        plan_price_id=prices["pro_yearly"].id, actor=owner, now=T0 + timedelta(days=1)
    )
    await PlanCatalogAdmin(db_session).retire_price(
        prices["pro_yearly"].id, PlanPriceRetire(reason="Re-priced."), actor=staff, now=T0
    )
    boundary = (await _subscription(db_session, tenant)).current_period_end
    await _worker(db_session, monkeypatch, paymob.provider()).run_once(
        now=boundary + timedelta(minutes=5)
    )
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == prices["pro_yearly"].id
    renewal = next(i for i in await _invoices(db_session, tenant) if i.purpose.value == "renewal")
    assert renewal.amount_due == PRO_YEARLY
    await _clean(db_session, tenant)


async def test_an_open_annual_page_settles_at_the_price_it_was_opened_at(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    _, invoice, payment = await _open(
        db_session, tenant, owner, paymob, prices["business_yearly"], now=T0
    )
    admin = PlanCatalogAdmin(db_session)
    await admin.retire_price(
        prices["business_yearly"].id, PlanPriceRetire(reason="Rise."), actor=staff, now=T0
    )
    await admin.create_price(
        prices["business_yearly"].plan_version_id,
        PlanPriceCreate(
            billing_interval=BillingInterval.YEARLY, amount=Decimal("3290.00"), reason="Rise."
        ),
        actor=staff,
        now=T0,
    )
    paid_at = T0 + timedelta(minutes=10)
    signed = _callback(payment, transaction=15_200_001)
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=paid_at) == APPLIED
    await db_session.refresh(invoice)
    subscription = await _subscription(db_session, tenant)
    assert (invoice.amount_paid, subscription.plan_price_id) == (
        BUSINESS_YEARLY,
        prices["business_yearly"].id,
    )
    await _clean(db_session, tenant)


async def test_a_migration_keeps_each_subscriber_on_their_billing_term(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas.platform_billing import PlanMigrationCreate

    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    paymob = Paymob()
    monthly, monthly_owner = await _workspace(db_session, name="Monthly")
    yearly, yearly_owner = await _workspace(db_session, name="Yearly")
    await _buy(
        db_session,
        monthly,
        monthly_owner,
        paymob,
        prices["pro_monthly"],
        now=T0,
        transaction=15_300_001,
    )
    await _buy(
        db_session,
        yearly,
        yearly_owner,
        paymob,
        prices["pro_yearly"],
        now=T0,
        transaction=15_300_002,
    )
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    admin = PlanCatalogAdmin(db_session)
    source = (await admin.versions(pro.id))[0]
    monthly_only = await admin.create_version(
        pro.id,
        PlanVersionCreate(
            prices=[PriceSpec(billing_interval=BillingInterval.MONTHLY, amount=Decimal("109"))],
            limits=SEVEN_PRO,
            expected_version=source.version,
            reason="Monthly only.",
        ),
        actor=staff,
        now=T0,
    )
    with pytest.raises(ValidationError, match="yearly"):
        await admin.schedule_migration(
            pro.id,
            PlanMigrationCreate(
                from_version=source.version,
                to_version=monthly_only.version,
                reason="Move them.",
                confirm=True,
            ),
            actor=staff,
        )
    both = await admin.create_version(
        pro.id,
        PlanVersionCreate(
            prices=[
                PriceSpec(billing_interval=BillingInterval.MONTHLY, amount=Decimal("119")),
                PriceSpec(billing_interval=BillingInterval.YEARLY, amount=Decimal("1190")),
            ],
            limits=SEVEN_PRO,
            expected_version=monthly_only.version,
            reason="Both terms.",
        ),
        actor=staff,
        now=T0,
    )
    await admin.schedule_migration(
        pro.id,
        PlanMigrationCreate(
            from_version=source.version, to_version=both.version, reason="Move.", confirm=True
        ),
        actor=staff,
    )
    worker = _worker(db_session, monkeypatch, paymob.provider())
    month_end = (await _subscription(db_session, monthly)).current_period_end
    await worker.run_once(now=month_end + timedelta(minutes=5))
    monthly_renewal = next(
        i for i in await _invoices(db_session, monthly) if i.purpose.value == "renewal"
    )
    assert (monthly_renewal.amount_due, monthly_renewal.billing_interval) == (
        Decimal("119.00"),
        BillingInterval.MONTHLY,
    )
    year_end = (await _subscription(db_session, yearly)).current_period_end
    await worker.run_once(now=year_end + timedelta(minutes=5))
    yearly_renewal = next(
        i for i in await _invoices(db_session, yearly) if i.purpose.value == "renewal"
    )
    assert (yearly_renewal.amount_due, yearly_renewal.billing_interval) == (
        Decimal("1190.00"),
        BillingInterval.YEARLY,
    ), "a yearly subscriber migrates to the yearly price"
    await _clean(db_session, monthly, yearly)


# ============================================================ top-ups


async def test_a_usage_top_up_on_an_annual_plan_ends_with_its_month(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session,
        tenant,
        owner,
        paymob,
        prices["business_yearly"],
        now=T0,
        transaction=16_000_001,
    )
    subscription = await _subscription(db_session, tenant)
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=10_000)
    bought_at = T0 + timedelta(days=14)
    checkout = CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider())
    started = await TopupService(db_session, tenant_id=tenant.id, checkout=checkout).start_checkout(
        item.id, actor=owner, idempotency_key=None, now=bought_at
    )
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    assert started.expires_at == subscription.usage_period_end, "the monthly cycle, not the year"
    assert started.expires_at != subscription.current_period_end
    signed = _callback(payment, transaction=16_000_002)
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=bought_at) == APPLIED
    during = await (await _entitlements(db_session, tenant, bought_at)).check(
        LimitKey.PERIOD_AI_TURNS, additional=0
    )
    assert during.limit == SEVEN_BUSINESS["period_ai_turns"] + 10_000
    after = await (
        await _entitlements(db_session, tenant, subscription.usage_period_end + timedelta(1))
    ).check(LimitKey.PERIOD_AI_TURNS, additional=0)
    assert after.limit == SEVEN_BUSINESS["period_ai_turns"], "no carry-over into November"
    assert len(paymob.pays()) == 0, "a top-up is paid once, at its own checkout"
    await _clean(db_session, tenant)


async def test_a_capacity_top_up_on_an_annual_plan_lasts_the_year_and_deletes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models.whatsapp import WhatsAppAccount

    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["pro_yearly"], now=T0, transaction=16_100_001
    )
    subscription = await _subscription(db_session, tenant)
    item = await product(db_session, entitlement=TopupEntitlement.WHATSAPP_NUMBERS, quantity=1)
    bought_at = T0 + timedelta(days=40)
    checkout = CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider())
    started = await TopupService(db_session, tenant_id=tenant.id, checkout=checkout).start_checkout(
        item.id, actor=owner, idempotency_key=None, now=bought_at
    )
    assert started.expires_at == subscription.current_period_end, "the paid year"
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    await _apply(
        db_session,
        tenant.id,
        paymob.provider(),
        _callback(payment, transaction=16_100_002),
        now=bought_at,
    )
    for index in range(2):
        db_session.add(
            WhatsAppAccount(
                tenant_id=tenant.id,
                phone_number_id=f"annual-{uuid.uuid4().hex[:12]}",
                waba_id="waba",
                display_phone_number=f"+2010000000{index}",
            )
        )
    await db_session.flush()
    mid = await (await _entitlements(db_session, tenant, T0 + timedelta(days=200))).check(
        LimitKey.WHATSAPP_NUMBERS, additional=0
    )
    assert (mid.limit, mid.used, mid.over_limit) == (2, 2, False), "valid all year"
    # The year ends: the top-up expires, both numbers stay, and a third is refused.
    after = subscription.current_period_end + timedelta(minutes=1)
    await _worker(db_session, monkeypatch, paymob.provider()).run_once(now=after)
    purchase = await db_session.scalar(
        select(TopupPurchase).where(TopupPurchase.id == started.purchase_id)
    )
    assert purchase is not None and purchase.status is TopupStatus.EXPIRED
    held = await db_session.scalar(
        select(func.count())
        .select_from(WhatsAppAccount)
        .where(WhatsAppAccount.tenant_id == tenant.id)
    )
    assert held == 2, "nothing is deleted when a capacity top-up expires"
    state = await (await _entitlements(db_session, tenant, after)).check(
        LimitKey.WHATSAPP_NUMBERS, additional=1
    )
    assert (state.limit, state.over_limit, state.allowed) == (1, True, False)
    await _clean(db_session, tenant)


# ============================================================ custom plans and offers


async def _custom_both(
    session: AsyncSession, tenant: Tenant, staff: User, *, select: BillingInterval
) -> Any:
    return await CustomPlanAdmin(session, settings=_settings()).create(
        tenant.id,
        CustomPlanCreate(
            code=f"ent-{uuid.uuid4().hex[:6]}",
            name="Enterprise Custom",
            prices=[
                PriceSpec(billing_interval=BillingInterval.MONTHLY, amount=Decimal("2500.00")),
                PriceSpec(billing_interval=BillingInterval.YEARLY, amount=Decimal("25000.00")),
            ],
            selected_billing_interval=select,
            period_messages=100_000,
            period_ai_turns=40_000,
            period_campaign_messages=50_000,
            storage_bytes=100 * GIB,
            whatsapp_numbers=5,
            team_members=30,
            knowledge_documents=3_000,
            financial_basis=CustomPlanBasis.CUSTOMER_CHECKOUT,
            reason="Negotiated terms.",
        ),
        actor=staff,
        now=T0,
    )


async def test_a_yearly_custom_offer_is_accepted_and_paid_as_a_year(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    created = await _custom_both(db_session, tenant, staff, select=BillingInterval.YEARLY)
    assert created.offer is not None
    offer_view = created.offer
    assert (offer_view.billing_interval, offer_view.price, offer_view.interval) == (
        BillingInterval.YEARLY,
        "25000.00",
        BillingInterval.YEARLY,
    )
    assert {limit.key.value: limit.limit for limit in offer_view.limits}["period_ai_turns"] == (
        40_000
    )
    assert offer_view.effective_period.if_paid_now_end.year == T0.year + 1
    accepted = await CustomPlanOfferService(
        db_session,
        tenant_id=tenant.id,
        checkout=CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider()),
    ).accept(offer_view.id, actor=owner, idempotency_key=None, now=T0)
    assert accepted.checkout.amount == Decimal("25000.00")
    assert paymob.intentions()[-1]["body"]["amount"] == 2_500_000
    payment = await db_session.get(Payment, accepted.checkout.payment_id)
    assert payment is not None
    paid_at = T0 + timedelta(minutes=3)
    signed = _callback(payment, transaction=17_000_001)
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=paid_at) == APPLIED
    assert await _apply(db_session, tenant.id, paymob.provider(), signed, now=paid_at) == DUPLICATE
    subscription = await _subscription(db_session, tenant)
    assert subscription.plan_price_id == offer_view.plan_price_id
    assert subscription.current_period_end == _year(paid_at)
    assert subscription.usage_period_end == paid_at.replace(month=11)
    await _clean(db_session, tenant)


async def test_an_offer_names_its_price_and_the_customer_cannot_change_it(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    created = await _custom_both(db_session, tenant, staff, select=BillingInterval.MONTHLY)
    assert created.offer is not None
    assert created.offer.billing_interval is BillingInterval.MONTHLY
    assert created.offer.price == "2500.00"
    # A version with two prices cannot be offered without naming one.
    another, _ = await _workspace(db_session, name="Another")
    with pytest.raises(ValidationError, match="plan_price_id"):
        await PlatformCustomPlanOffers(db_session).offer(
            tenant.id,
            CustomPlanOfferCreate(plan_version_id=created.version.id, reason="Ambiguous."),
            actor=staff,
            now=T0,
        )
    from pydantic import ValidationError as SchemaError

    from app.schemas.custom_plan import CustomPlanOfferAccept

    for tamper in (
        {"billing_interval": "monthly"},
        {"plan_price_id": str(uuid.uuid4())},
        {"amount": "1.00"},
    ):
        with pytest.raises(SchemaError):
            CustomPlanOfferAccept.model_validate(tamper)
    assert another.id != tenant.id


async def test_a_complimentary_yearly_assignment_moves_no_money(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, _ = await _workspace(db_session)
    subscription = await _subscription(db_session, tenant)
    paymob = Paymob()
    changed = await PlatformBillingOperations(db_session, settings=_settings()).change_plan(
        subscription.id,
        SubscriptionChangePlan(
            plan_version_id=prices["business_yearly"].plan_version_id,
            plan_price_id=prices["business_yearly"].id,
            mode=ChangeMode.NOW,
            financial_basis=FinancialBasis.COMPLIMENTARY,
            reason="Launch partner.",
            expected_revision=subscription.revision,
        ),
        actor=staff,
        now=T0,
    )
    assert changed.billing_interval is BillingInterval.YEARLY
    assert changed.current_period_end == _year(T0)
    assert changed.usage_period_end == T0.replace(month=11)
    assert await _invoices(db_session, tenant) == [], "no invoice"
    assert paymob.requests == [], "no Paymob"
    payments = await db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant.id)
    )
    assert payments == 0
    audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant.id)
        .where(AuditLog.action == AuditAction.SUBSCRIPTION_COMPLIMENTARY_GRANT)
    )
    assert audit is not None
    await _clean(db_session, tenant)


# ============================================================ isolation


async def test_another_workspaces_custom_price_is_refused_everywhere(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    await _catalogue(db_session, staff)
    alpha, _ = await _workspace(db_session, name="Alpha")
    beta, beta_owner = await _workspace(db_session, name="Beta")
    created = await _custom_both(db_session, alpha, staff, select=BillingInterval.YEARLY)
    assert created.offer is not None
    foreign_price = created.offer.plan_price_id
    paymob = Paymob()

    with pytest.raises(ValidationError, match="No such price"):
        await CheckoutService(db_session, tenant_id=beta.id, provider=paymob.provider()).start(
            plan_price_id=foreign_price, actor=beta_owner, now=T0
        )
    with pytest.raises(ValidationError, match="No such price"):
        await SubscriptionService(db_session, tenant_id=beta.id).request_plan(
            plan_price_id=foreign_price, actor=beta_owner, now=T0
        )
    with pytest.raises(ValidationError):
        await PlatformCustomPlanOffers(db_session).offer(
            beta.id,
            CustomPlanOfferCreate(plan_price_id=foreign_price, reason="Wrong company."),
            actor=staff,
            now=T0,
        )
    beta_subscription = await _subscription(db_session, beta)
    with pytest.raises(DBAPIError, match="custom_plan_not_available_for_workspace"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE subscriptions SET plan_id = v.plan_id, plan_version_id = v.id,"
                    " plan_price_id = :price FROM plan_versions v"
                    " WHERE v.id = :version AND subscriptions.id = :sub"
                ),
                {
                    "price": foreign_price,
                    "version": created.version.id,
                    "sub": beta_subscription.id,
                },
            )
    await _clean(db_session, alpha, beta)


async def test_the_database_refuses_a_price_of_another_version(
    db_session: AsyncSession,
) -> None:
    staff = await _staff(db_session)
    prices = await _catalogue(db_session, staff)
    tenant, owner = await _workspace(db_session)
    paymob = Paymob()
    await _buy(
        db_session, tenant, owner, paymob, prices["pro_monthly"], now=T0, transaction=18_000_001
    )
    subscription = await _subscription(db_session, tenant)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE subscriptions SET plan_price_id = :price WHERE id = :id"),
                {"price": prices["business_yearly"].id, "id": subscription.id},
            )
    invoice = (await _invoices(db_session, tenant))[0]
    with pytest.raises(DBAPIError, match="keeps the price"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE invoices SET plan_price_id = :price WHERE id = :id"),
                {"price": prices["pro_yearly"].id, "id": invoice.id},
            )
    with pytest.raises(DBAPIError, match="exactly the price"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO invoices (id, tenant_id, subscription_id, status, purpose,"
                    " plan_code, plan_version_id, plan_price_id, billing_interval, interval_count,"
                    " amount_due, amount_paid, currency, period_start, period_end, lines,"
                    " collection_attempts, revision)"
                    " VALUES (gen_random_uuid(), :t, :s, 'open', 'checkout', 'pro', :v, :p,"
                    " 'yearly', 1, 1.00, 0, 'EGP', now(), now() + interval '1 year', '[]', 0, 1)"
                ),
                {
                    "t": tenant.id,
                    "s": subscription.id,
                    "v": prices["pro_yearly"].plan_version_id,
                    "p": prices["pro_yearly"].id,
                },
            )
