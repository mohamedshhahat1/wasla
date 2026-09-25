"""The platform billing control plane, over HTTP (BILL-12, BILL-13, BILL-15).

Three things are proved here:

1. **Authority.** Every `/platform/billing/*` mutation refuses anonymous
   callers (401) and every tenant role - member, admin and owner alike - with
   403; platform admins and owners get through, and deleting a plan is
   owner-only.
2. **Catalogue discipline.** Validation refuses bad money and unknown
   entitlement keys; stale edits are 409; publishing a version changes nobody
   already subscribed; referenced plans cannot be deleted.
3. **Operations.** Manual payments, voids, refunds and plan changes validate
   what they are given, are serialised by revision, and are audited - and no
   response carries a credential.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import TokenClaims, TokenType
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import BillingAdjustment, Plan, Subscription, SubscriptionStatus
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.invoice import InvoiceStatus, Payment, PaymentStatus
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing import paymob
from app.main import create_app
from app.services.subscription_service import SubscriptionService
from tests.billing_fixtures import renewal_invoice
from tests.integration.plan_catalogue import own_plan
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY
from tests.paymob_orders import order_from_request

pytestmark = pytest.mark.integration

BASE = "/api/v1/platform/billing"
NOW = datetime(2026, 9, 1, 10, tzinfo=UTC)
FORBIDDEN_IN_RESPONSES = (
    "provider_token",
    "token_fingerprint",
    "client_secret",
    "payment_key",
    "hmac",
    "secret_key",
    "api_key",
)


class _Infra:
    def __init__(self) -> None:
        self.commands = self

    @property
    def client(self) -> _Infra:
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


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        jwt_secret="platform-billing-api-secret-not-for-deployment",
        rate_limit_enabled=False,
        billing_provider="paymob",
        paymob_secret_key="egy_sk_test_platform",
        paymob_public_key="egy_pk_test_platform",
        paymob_hmac_secret="platform-billing-hmac",
        paymob_integration_ids=[4097558],
        app_public_url="https://app.wasla.test",
        credential_encryption_keys=[ENCRYPTION_KEY],
        payment_token_fingerprint_key=FINGERPRINT_KEY,
        default_plan_code="starter",
    )


class RefundDesk:
    """Paymob's refund endpoint at the socket, counting what it was asked."""

    def __init__(self) -> None:
        self.refunds: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content or b"{}")
        if "void_refund/refund" in str(request.url):
            self.refunds.append(body)
            return httpx.Response(
                200, json={"id": 880000000 + len(self.refunds), "success": True, "pending": True}
            )
        if "/v1/intention" in str(request.url):
            return httpx.Response(
                201,
                json={
                    "id": "pi_test_platform",
                    "client_secret": "egy_csk_test_platform",
                    "intention_order_id": order_from_request(request),
                },
            )
        return httpx.Response(404, json={})


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> RefundDesk:
    desk = RefundDesk()
    original = paymob.PaymobProvider.__init__

    def patched(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("transport", httpx.MockTransport(desk.handler))
        original(self, **kwargs)

    monkeypatch.setattr(paymob.PaymobProvider, "__init__", patched)
    return desk


@pytest.fixture
def app(db_session: AsyncSession, desk: RefundDesk) -> Iterator[FastAPI]:
    application = create_app(_settings())
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


async def _user(session: AsyncSession, *, role: PlatformRole | None = None) -> User:
    user = User(
        email=f"user-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        email_verified_at=datetime.now(UTC),
        platform_role=role,
    )
    session.add(user)
    await session.flush()
    return user


def _act_as(app: FastAPI, user: User | None) -> None:
    if user is None:
        app.dependency_overrides.pop(get_current_user, None)
        return
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=user, claims=claims)


async def _workspace(session: AsyncSession) -> tuple[Tenant, User]:
    await own_plan(session, code="starter", price=Decimal("0.00"), limits={"agents": 1})
    await own_plan(session, code="pro", price=Decimal("99.00"), limits={"agents": 5})
    tenant = Tenant(name="Customer", slug=f"cust-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await _user(session)
    session.add(
        Membership(
            tenant_id=tenant.id,
            user_id=owner.id,
            role=TenantRole.TENANT_OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()
    await SubscriptionService(session, tenant_id=tenant.id).start(
        plan_code="starter", now=NOW, self_service=False
    )
    return tenant, owner


def _clean(body: object) -> None:
    text = str(body)
    for marker in FORBIDDEN_IN_RESPONSES:
        assert marker not in text, f"{marker} leaked into a platform billing response"


# ---------------------------------------------------------------- authority

MUTATIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("post", "/plans", {}),
    ("patch", f"/plans/{uuid.uuid4()}", {}),
    ("post", f"/plans/{uuid.uuid4()}/activate", {}),
    ("post", f"/plans/{uuid.uuid4()}/deactivate", {}),
    ("post", f"/plans/{uuid.uuid4()}/versions", {}),
    ("post", f"/plans/{uuid.uuid4()}/versions/preview", {}),
    ("post", f"/plans/{uuid.uuid4()}/migrations", {}),
    ("post", f"/subscriptions/{uuid.uuid4()}/change-plan", {}),
    ("post", f"/subscriptions/{uuid.uuid4()}/cancel", {}),
    ("post", f"/subscriptions/{uuid.uuid4()}/resume", {}),
    ("post", f"/invoices/{uuid.uuid4()}/payments", {}),
    ("post", f"/invoices/{uuid.uuid4()}/void", {}),
    ("post", f"/payments/{uuid.uuid4()}/refund", {}),
    ("post", f"/reconciliation/{uuid.uuid4()}/run", {}),
    ("post", f"/incidents/{uuid.uuid4()}/resolve", {}),
    ("delete", f"/plans/{uuid.uuid4()}?reason=cleanup", {}),
]
READS = ["/plans", "/features", "/subscriptions", "/invoices", "/payments", "/reconciliation"]


@pytest.mark.parametrize(
    "role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN, TenantRole.TENANT_OWNER]
)
async def test_every_tenant_role_is_refused_everywhere(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: TenantRole
) -> None:
    tenant, _ = await _workspace(db_session)
    user = await _user(db_session)
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=user.id, role=role, status=MembershipStatus.ACTIVE)
    )
    await db_session.flush()
    _act_as(app, user)

    for method, path, body in MUTATIONS:
        kwargs = {} if method == "delete" else {"json": body}
        response = await getattr(http, method)(f"{BASE}{path}", **kwargs)
        assert response.status_code == 403, (method, path, response.status_code)
    for path in READS:
        assert (await http.get(f"{BASE}{path}")).status_code == 403, path


async def test_anonymous_callers_are_refused(app: FastAPI, http: AsyncClient) -> None:
    _act_as(app, None)
    for method, path, body in MUTATIONS:
        kwargs = {} if method == "delete" else {"json": body}
        response = await getattr(http, method)(f"{BASE}{path}", **kwargs)
        assert response.status_code == 401, (method, path)


@pytest.mark.parametrize("role", [PlatformRole.PLATFORM_ADMIN, PlatformRole.PLATFORM_OWNER])
async def test_platform_staff_reach_every_route(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: PlatformRole
) -> None:
    _act_as(app, await _user(db_session, role=role))
    for method, path, body in MUTATIONS:
        if method == "delete":
            continue
        kwargs = {"json": body}
        response = await getattr(http, method)(f"{BASE}{path}", **kwargs)
        # Past authorization: the empty body is a validation error, or the
        # random id is not found. Never 401/403.
        assert response.status_code in (404, 409, 422), (method, path, response.status_code)
    for path in READS:
        assert (await http.get(f"{BASE}{path}")).status_code == 200, path


async def test_only_a_platform_owner_may_delete_a_plan(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    created = await http.post(f"{BASE}/plans", json=_plan_body(code="doomed"))
    assert created.status_code == 201
    plan_id = created.json()["id"]

    refused = await http.delete(f"{BASE}/plans/{plan_id}", params={"reason": "not needed"})
    assert refused.status_code == 403

    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_OWNER))
    deleted = await http.delete(f"{BASE}/plans/{plan_id}", params={"reason": "not needed"})
    assert deleted.status_code == 204
    assert await db_session.get(Plan, uuid.UUID(plan_id)) is None


# ----------------------------------------------------------------- catalogue


def _plan_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": f"plan-{uuid.uuid4().hex[:6]}",
        "name": "Growth",
        "price": "149.00",
        "currency": "EGP",
        "interval": "monthly",
        "limits": {"agents": 7, "whatsapp_numbers": None},
        "reason": "A new tier.",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "invalid",
    [
        {"price": "-5.00"},
        {"currency": "ZZZ"},
        {"trial_days": 14},
        {"limits": {"not_a_feature": 3}},
        {"limits": {"agents": -1}},
        {"limits": {"agents": 10**16}},
        {"interval": "weekly"},
        {"price": "99999999.00"},
        {"code": "Bad Code!"},
        {"reason": ""},
    ],
)
async def test_an_invalid_plan_is_refused(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, invalid: dict[str, Any]
) -> None:
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    response = await http.post(f"{BASE}/plans", json=_plan_body(**invalid))
    assert response.status_code == 422, response.text


async def test_a_duplicate_plan_code_is_a_conflict(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    body = _plan_body(code="twice")
    assert (await http.post(f"{BASE}/plans", json=body)).status_code == 201
    assert (await http.post(f"{BASE}/plans", json=body)).status_code == 409


async def test_the_catalogue_lifecycle_is_versioned_serialised_and_audited(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Create, publish a version, refuse a stale one, preview, migrate, retire."""
    staff = await _user(db_session, role=PlatformRole.PLATFORM_ADMIN)
    _act_as(app, staff)
    created = (await http.post(f"{BASE}/plans", json=_plan_body(code="growth"))).json()
    plan_id = created["id"]
    assert created["current_version"]["version"] == 1
    assert created["current_version"]["price"] == "149.00"
    limits = {item["key"]: item["limit"] for item in created["current_version"]["limits"]}
    assert limits["agents"] == 7 and limits["whatsapp_numbers"] is None

    listed = await http.get(f"{BASE}/plans", params={"code": "growth", "limit": 10})
    assert listed.status_code == 200 and listed.json()["total"] == 1

    version = {
        "price": "199.00",
        "currency": "EGP",
        "interval": "monthly",
        "limits": {"agents": 9},
        "expected_version": 1,
        "reason": "Price rise.",
    }
    published = await http.post(f"{BASE}/plans/{plan_id}/versions", json=version)
    assert published.status_code == 201 and published.json()["version"] == 2
    stale = await http.post(f"{BASE}/plans/{plan_id}/versions", json=version)
    assert stale.status_code == 409, "Admin A edited from version 1 after B published 2"

    preview = await http.post(
        f"{BASE}/plans/{plan_id}/versions/preview",
        json={"price": "249.00", "currency": "EGP", "interval": "monthly", "limits": {"agents": 2}},
    )
    assert preview.status_code == 200
    assert preview.json()["current_version"] == 2
    versions = (await http.get(f"{BASE}/plans/{plan_id}/versions")).json()
    assert [item["version"] for item in versions] == [2, 1], "a preview writes nothing"

    migration = await http.post(
        f"{BASE}/plans/{plan_id}/migrations",
        json={"from_version": 1, "to_version": 2, "reason": "Move everyone.", "confirm": True},
    )
    assert migration.status_code == 200 and migration.json()["scheduled"] is True
    again = await http.post(
        f"{BASE}/plans/{plan_id}/migrations",
        json={"from_version": 1, "to_version": 2, "reason": "Twice.", "confirm": True},
    )
    assert again.status_code == 409

    plan = (await http.get(f"{BASE}/plans/{plan_id}")).json()
    stale_patch = await http.patch(
        f"{BASE}/plans/{plan_id}",
        json={"name": "Growth+", "expected_revision": plan["revision"] - 1, "reason": "Rename."},
    )
    assert stale_patch.status_code == 409
    renamed = await http.patch(
        f"{BASE}/plans/{plan_id}",
        json={"name": "Growth+", "expected_revision": plan["revision"], "reason": "Rename."},
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Growth+"

    retired = await http.post(
        f"{BASE}/plans/{plan_id}/deactivate",
        json={"expected_revision": renamed.json()["revision"], "reason": "Retire."},
    )
    assert retired.status_code == 200 and retired.json()["is_active"] is False

    actions = (
        await db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.target_id == uuid.UUID(plan_id))
            .order_by(AuditLog.occurred_at)
        )
    ).all()
    assert set(actions) >= {
        AuditAction.BILLING_PLAN_CREATED,
        AuditAction.BILLING_PLAN_VERSION_CREATED,
        AuditAction.BILLING_PLAN_MIGRATION_SCHEDULED,
        AuditAction.BILLING_PLAN_UPDATED,
        AuditAction.BILLING_PLAN_DEACTIVATED,
    }
    entry = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.BILLING_PLAN_VERSION_CREATED)
    )
    assert entry is not None and entry.meta is not None
    assert entry.meta["actor_role"] == "platform_admin"
    assert entry.meta["before"]["price"] == "149.00"
    assert entry.meta["after"]["price"] == "199.00"
    assert entry.meta["reason"] == "Price rise."


async def test_a_referenced_plan_cannot_be_deleted(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    await _workspace(db_session)
    starter = await db_session.scalar(select(Plan).where(Plan.code == "starter"))
    assert starter is not None
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_OWNER))
    response = await http.delete(f"{BASE}/plans/{starter.id}", params={"reason": "cleanup"})
    assert response.status_code == 409


async def test_the_feature_catalogue_names_only_real_keys(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    from app.db.models.billing import LimitKey

    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    features = (await http.get(f"{BASE}/features")).json()
    assert {item["key"] for item in features} == {key.value for key in LimitKey}
    by_key = {item["key"]: item for item in features}
    assert by_key["period_messages"]["kind"] == "meter_only"
    assert by_key["agents"]["concurrency_safe"] is True
    assert {item["unlimited"] for item in features} == {"null"}


# -------------------------------------------------------------- operations


async def test_an_operator_cannot_grant_a_priced_plan_for_nothing(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 62/63: `now` to a priced version must name its funding."""
    tenant, _ = await _workspace(db_session)
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    from app.services.plan_catalog import PlanCatalog

    version = await PlanCatalog(db_session).current_version(pro)
    assert version is not None
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    base = {
        "plan_version_id": str(version.id),
        "mode": "now",
        "reason": "Customer asked by phone.",
        "expected_revision": subscription.revision,
    }

    unfunded = await http.post(f"{BASE}/subscriptions/{subscription.id}/change-plan", json=base)
    assert unfunded.status_code == 422

    wrong = await http.post(
        f"{BASE}/subscriptions/{subscription.id}/change-plan",
        json={
            **base,
            "financial_basis": "manual_payment",
            "manual_payment": {"amount": "50.00", "currency": "EGP", "method": "bank_transfer"},
        },
    )
    assert wrong.status_code == 422

    stale = await http.post(
        f"{BASE}/subscriptions/{subscription.id}/change-plan",
        json={**base, "financial_basis": "complimentary", "expected_revision": 999},
    )
    assert stale.status_code == 409

    comp = await http.post(
        f"{BASE}/subscriptions/{subscription.id}/change-plan",
        json={**base, "financial_basis": "complimentary"},
    )
    assert comp.status_code == 200, comp.text
    assert comp.json()["plan_code"] == "pro"
    grant = await db_session.scalar(
        select(BillingAdjustment).where(BillingAdjustment.subscription_id == subscription.id)
    )
    assert grant is not None and grant.reason == "Customer asked by phone."
    assert (
        await db_session.scalar(select(Payment).where(Payment.tenant_id == tenant.id))
    ) is None, "a complimentary grant is never a fabricated payment"


async def test_a_manual_payment_is_validated_and_settles_once(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    tenant, _ = await _workspace(db_session)
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    subscription.plan_id = pro.id
    subscription.plan_version_id = None
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()
    invoice = await renewal_invoice(db_session, subscription=subscription, plan=pro)
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    path = f"{BASE}/invoices/{invoice.id}/payments"
    body = {
        "amount": "99.00",
        "currency": "EGP",
        "method": "bank_transfer",
        "reference": "BT-1",
        "reason": "Transfer seen in the bank statement.",
        "expected_revision": invoice.revision,
    }

    assert (await http.post(path, json={**body, "currency": "USD"})).status_code == 422
    assert (await http.post(path, json={**body, "amount": "150.00"})).status_code == 422
    assert (await http.post(path, json={**body, "expected_revision": 99})).status_code == 409

    paid = await http.post(path, json=body)
    assert paid.status_code == 201, paid.text
    _clean(paid.json())
    await db_session.refresh(invoice)
    await db_session.refresh(subscription)
    assert invoice.status is InvoiceStatus.PAID
    assert subscription.status is SubscriptionStatus.ACTIVE

    again = await http.post(path, json={**body, "expected_revision": invoice.revision})
    assert again.status_code == 409


async def test_reading_one_workspaces_billing_is_audited_against_it(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """ADR-095 for the billing plane: a single-row read names its workspace."""
    tenant, _ = await _workspace(db_session)
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    staff = await _user(db_session, role=PlatformRole.PLATFORM_ADMIN)
    _act_as(app, staff)

    for path in (f"/subscriptions/{subscription.id}", f"/subscriptions/{subscription.id}/timeline"):
        assert (await http.get(f"{BASE}{path}")).status_code == 200, path

    entries = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PLATFORM_BILLING_READ)
            .where(AuditLog.actor_id == staff.id)
        )
    ).all()
    assert sorted(str((entry.meta or {}).get("resource")) for entry in entries) == [
        "billing.subscription",
        "billing.subscription_timeline",
    ]
    single = next(e for e in entries if (e.meta or {}).get("resource") == "billing.subscription")
    assert single.target_id == tenant.id


async def test_a_platform_refund_is_bounded_and_confirmed_later(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, desk: RefundDesk
) -> None:
    """BILL-13: amount > 0, <= remaining, succeeded only, one request at a time."""
    tenant, _ = await _workspace(db_session)
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    pro = await db_session.scalar(select(Plan).where(Plan.code == "pro"))
    assert pro is not None
    invoice = await renewal_invoice(db_session, subscription=subscription, plan=pro)
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.SUCCEEDED,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
        provider_reference="541130792",
        refunded_amount=Decimal("0.00"),
    )
    failed = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.FAILED,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
        refunded_amount=Decimal("0.00"),
    )
    db_session.add_all([payment, failed])
    await db_session.flush()
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_OWNER))
    body = {"amount": "40.00", "currency": "EGP", "reason": "Goodwill.", "expected_revision": 1}

    assert (
        await http.post(f"{BASE}/payments/{payment.id}/refund", json={**body, "amount": "120.00"})
    ).status_code == 422
    assert (await http.post(f"{BASE}/payments/{failed.id}/refund", json=body)).status_code == 409
    assert (await http.post(f"{BASE}/payments/{uuid.uuid4()}/refund", json=body)).status_code == 404

    accepted = await http.post(f"{BASE}/payments/{payment.id}/refund", json=body)
    assert accepted.status_code == 202, accepted.text
    _clean(accepted.json())
    assert accepted.json()["refund_pending"] is True
    assert accepted.json()["status"] == "succeeded", "money moves on the callback, not the request"
    assert desk.refunds == [{"transaction_id": "541130792", "amount_cents": 4000}]

    duplicate = await http.post(
        f"{BASE}/payments/{payment.id}/refund",
        json={**body, "expected_revision": accepted.json()["revision"]},
    )
    assert duplicate.status_code == 409
    assert len(desk.refunds) == 1


async def test_reads_never_carry_credentials(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    await _workspace(db_session)
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    for path in READS:
        response = await http.get(f"{BASE}{path}", params={"limit": 100})
        assert response.status_code == 200
        _clean(response.json())
    assert (await http.get(f"{BASE}/payments", params={"limit": 101})).status_code == 422


async def test_search_text_is_validated_before_sql(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """SEC-04 is not reopened here: NUL is a 422, never a driver error."""
    _act_as(app, await _user(db_session, role=PlatformRole.PLATFORM_ADMIN))
    response = await http.get(f"{BASE}/plans", params={"code": "pro\x00"})
    assert response.status_code == 422
