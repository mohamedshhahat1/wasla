"""Custom plans and top-ups over HTTP: authority, isolation, contracts (ADR-113).

The whole application is built - the real dependency graph, the real
authorization dependencies, the real serialisers and the real entitlement
engine. Only the session, Paymob's socket and *who is calling* are substituted.

Proved here:

1. **Authority.** Every new `/platform/billing/*` route refuses anonymous callers
   (401) and every tenant role, a workspace owner included (403). Platform
   admins and owners get through; deleting a top-up product is owner-only.
2. **The TENANT binding.** A custom plan is on its own workspace's catalogue and
   nobody else's; another workspace cannot buy it, and the platform cannot
   assign it elsewhere (422 `custom_plan_not_available_for_workspace`).
3. **Contracts.** Validation refuses unknown keys, negative quantities and
   other currencies; stale revisions are 409; the breakdown of every limit is
   served; no response carries a credential.
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
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    ActiveWorkspace,
    CurrentUser,
    get_active_workspace,
    get_current_user,
)
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import TokenClaims, TokenType
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import Plan, PlanScope, PlanVersion
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement
from app.db.models.user import User
from app.integrations.billing import paymob
from app.main import create_app
from tests.integration.topup_harness import GIB, base_now, catalogue, product, workspace
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY
from tests.paymob_orders import order_from_request

pytestmark = pytest.mark.integration

PLATFORM = "/api/v1/platform/billing"
BILLING = "/api/v1/billing"
FORBIDDEN_IN_RESPONSES = (
    "provider_token",
    "token_fingerprint",
    "client_secret",
    "payment_key",
    "hmac",
    "secret_key",
    "api_key",
)
CUSTOM_ERROR = "custom_plan_not_available_for_workspace"


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
        jwt_secret="commercial-api-secret-not-for-deployment",
        rate_limit_enabled=False,
        billing_provider="paymob",
        paymob_secret_key="egy_sk_test_commercial",
        paymob_public_key="egy_pk_test_commercial",
        paymob_hmac_secret="commercial-hmac",
        paymob_integration_ids=[4097558],
        app_public_url="https://app.wasla.test",
        credential_encryption_keys=[ENCRYPTION_KEY],
        payment_token_fingerprint_key=FINGERPRINT_KEY,
        default_plan_code="starter",
    )


class Intentions:
    """Paymob's intention endpoint at the socket, counting pages opened."""

    def __init__(self) -> None:
        self.count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "/v1/intention" in str(request.url):
            self.count += 1
            return httpx.Response(
                201,
                json={
                    "id": f"pi_test_{uuid.uuid4().hex[:10]}",
                    "client_secret": f"egy_csk_test_{uuid.uuid4().hex[:10]}",
                    "intention_order_id": order_from_request(request),
                },
            )
        return httpx.Response(404, json={})


@pytest.fixture
def intentions(monkeypatch: pytest.MonkeyPatch) -> Intentions:
    desk = Intentions()
    original = paymob.PaymobProvider.__init__

    def patched(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("transport", httpx.MockTransport(desk.handler))
        original(self, **kwargs)

    monkeypatch.setattr(paymob.PaymobProvider, "__init__", patched)
    return desk


@pytest.fixture
def app(db_session: AsyncSession, intentions: Intentions) -> Iterator[FastAPI]:
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


def _as_platform(app: FastAPI, user: User | None) -> None:
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


def _as_member(app: FastAPI, tenant: Tenant, user: User, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=user,
        membership=Membership(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=tenant.id,
            role=role,
            status=MembershipStatus.ACTIVE,
        ),
        tenant=tenant,
    )


def _clean(body: object) -> None:
    rendered = str(body)
    for marker in FORBIDDEN_IN_RESPONSES:
        assert marker not in rendered, f"{marker} leaked into a response"


def _custom_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": f"abc-{uuid.uuid4().hex[:6]}",
        "name": "ABC Enterprise",
        "price": "1500.00",
        "currency": "EGP",
        "billing_interval": "monthly",
        "period_messages": 100_000,
        "period_ai_turns": 40_000,
        "period_campaign_messages": 50_000,
        "storage_bytes": 100 * GIB,
        "whatsapp_numbers": 5,
        "team_members": 30,
        "knowledge_documents": 3_000,
        "reason": "Negotiated enterprise terms.",
    }
    body.update(overrides)
    return body


async def _staff(app: FastAPI, session: AsyncSession, role: PlatformRole) -> User:
    user = await _user(session, role=role)
    _as_platform(app, user)
    return user


# ------------------------------------------------------------------ authority

NEW_MUTATIONS: list[tuple[str, str]] = [
    ("post", f"/tenants/{uuid.uuid4()}/custom-plan/preview"),
    ("post", f"/tenants/{uuid.uuid4()}/custom-plan"),
    ("post", f"/tenants/{uuid.uuid4()}/topups/grant"),
    ("post", "/topups"),
    ("patch", f"/topups/{uuid.uuid4()}"),
    ("post", f"/topups/{uuid.uuid4()}/activate"),
    ("post", f"/topups/{uuid.uuid4()}/deactivate"),
    ("post", f"/topup-purchases/{uuid.uuid4()}/refund-review"),
    ("delete", f"/topups/{uuid.uuid4()}?reason=cleanup"),
]
NEW_READS = [
    "/topups",
    f"/topups/{uuid.uuid4()}",
    "/topup-purchases",
    f"/topup-purchases/{uuid.uuid4()}",
    f"/tenants/{uuid.uuid4()}/summary",
]


@pytest.mark.parametrize(
    "role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN, TenantRole.TENANT_OWNER]
)
async def test_every_tenant_role_is_refused_every_new_platform_route(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: TenantRole
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    user = await _user(db_session)
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=user.id, role=role, status=MembershipStatus.ACTIVE)
    )
    await db_session.flush()
    _as_platform(app, user)
    for method, path in NEW_MUTATIONS:
        kwargs: dict[str, Any] = {} if method == "delete" else {"json": {}}
        response = await getattr(http, method)(f"{PLATFORM}{path}", **kwargs)
        assert response.status_code == 403, (method, path, response.status_code)
    for path in NEW_READS:
        assert (await http.get(f"{PLATFORM}{path}")).status_code == 403, path


async def test_anonymous_callers_are_refused_every_new_platform_route(
    app: FastAPI, http: AsyncClient
) -> None:
    _as_platform(app, None)
    for method, path in NEW_MUTATIONS:
        kwargs: dict[str, Any] = {} if method == "delete" else {"json": {}}
        assert (await getattr(http, method)(f"{PLATFORM}{path}", **kwargs)).status_code == 401
    for path in NEW_READS:
        assert (await http.get(f"{PLATFORM}{path}")).status_code == 401


@pytest.mark.parametrize("role", [PlatformRole.PLATFORM_ADMIN, PlatformRole.PLATFORM_OWNER])
async def test_platform_staff_reach_every_new_route(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: PlatformRole
) -> None:
    await _staff(app, db_session, role)
    for method, path in NEW_MUTATIONS:
        if method == "delete":
            continue
        response = await getattr(http, method)(f"{PLATFORM}{path}", json={})
        assert response.status_code in (404, 409, 422), (method, path, response.status_code)
    for path in NEW_READS:
        assert (await http.get(f"{PLATFORM}{path}")).status_code in (200, 404), path


async def test_only_a_platform_owner_may_hard_delete_a_topup_product(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    created = await http.post(
        f"{PLATFORM}/topups",
        json={
            "code": f"doomed-{uuid.uuid4().hex[:6]}",
            "name": "Doomed",
            "entitlement_key": "period_ai_turns",
            "quantity": 100,
            "price": "10.00",
            "currency": "EGP",
            "reason": "Trying it out.",
        },
    )
    assert created.status_code == 201, created.text
    path = f"{PLATFORM}/topups/{created.json()['id']}"
    assert (await http.delete(path, params={"reason": "not needed"})).status_code == 403
    await _staff(app, db_session, PlatformRole.PLATFORM_OWNER)
    assert (await http.delete(path, params={"reason": "not needed"})).status_code == 204


# --------------------------------------------------------------- custom plans


async def test_a_custom_plan_preview_shows_everything_and_writes_nothing(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 10: current vs proposed, usage, what falls below it, the next charge."""
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    plans_before = await db_session.scalar(select(func.count()).select_from(Plan))

    body = _custom_body(team_members=0, assignment_mode="next_renewal")
    del body["reason"]  # a preview writes nothing, so it records no reason
    response = await http.post(f"{PLATFORM}/tenants/{tenant.id}/custom-plan/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["tenant"]["id"] == str(tenant.id)
    assert preview["current_plan"]["code"] == "pro"
    assert (preview["proposed_price"], preview["currency"], preview["interval"]) == (
        "1500.00",
        "EGP",
        "monthly",
    )
    rows = {row["key"]: row for row in preview["limits"]}
    assert set(rows) == {key.value for key in TopupEntitlement}
    assert rows["period_ai_turns"]["current"] == 5_000
    assert rows["period_ai_turns"]["proposed"] == 40_000
    assert rows["period_ai_turns"]["difference"] == 35_000
    # One owner already holds a seat; a proposal of zero is below usage.
    assert rows["team_members"]["used"] == 1
    assert rows["team_members"]["below_current_usage"] is True
    assert preview["inherited_limits"]["agents"] == 5
    assert preview["effective_mode"] == "next_renewal"
    effective = datetime.fromisoformat(preview["effective_at"].replace("Z", "+00:00"))
    assert effective == subscription.current_period_end
    assert preview["estimated_next_charge"]["basis"] == "renewal"
    assert preview["estimated_next_charge"]["amount"] == "1500.00"
    assert await db_session.scalar(select(func.count()).select_from(Plan)) == plans_before


async def test_a_custom_plan_is_one_workspaces_and_nobody_elses(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 5, 69, 70, CT-01/CT-02/CT-04: created with all seven limits, listed
    for its owner only, unbuyable and unassignable anywhere else.
    """
    now = base_now()
    await catalogue(db_session)
    alpha, alpha_owner, _ = await workspace(db_session, now=now, name="Alpha")
    beta, beta_owner, beta_subscription = await workspace(db_session, now=now, name="Beta")
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)

    body = _custom_body()
    created = await http.post(f"{PLATFORM}/tenants/{alpha.id}/custom-plan", json=body)
    assert created.status_code == 201, created.text
    result = created.json()
    _clean(result)
    assert result["plan"]["scope"] == "tenant"
    assert result["plan"]["tenant_id"] == str(alpha.id)
    assert result["plan"]["is_custom"] is True and result["plan"]["is_public"] is False
    limits = {row["key"]: row["limit"] for row in result["version"]["limits"]}
    for key in TopupEntitlement:
        assert limits[key.value] == body[key.value], key
    assert limits["agents"] == 5, "agents are inherited, never left unlimited"
    assert result["assignment"]["status"] == "not_assigned"
    audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_CUSTOM_PLAN_CREATED)
        .where(AuditLog.target_id == uuid.UUID(result["plan"]["id"]))
    )
    assert audit is not None and audit.tenant_id == alpha.id

    # Alpha's catalogue carries it, marked; Beta's does not.
    _as_member(app, alpha, alpha_owner, TenantRole.MEMBER)
    alpha_plans = {row["code"]: row for row in (await http.get(f"{BILLING}/plans")).json()}
    assert alpha_plans[body["code"]]["is_custom"] is True
    _as_member(app, beta, beta_owner, TenantRole.MEMBER)
    assert body["code"] not in {row["code"] for row in (await http.get(f"{BILLING}/plans")).json()}

    # Beta cannot buy it: indistinguishable from a plan that does not exist.
    _as_member(app, beta, beta_owner, TenantRole.TENANT_OWNER)
    refused = await http.post(f"{BILLING}/checkout", json={"plan_code": body["code"]})
    assert refused.status_code == 422
    missing = await http.post(f"{BILLING}/checkout", json={"plan_code": "no-such-plan"})
    assert refused.json()["error"]["message"] == missing.json()["error"]["message"]
    # Alpha's owner can.
    _as_member(app, alpha, alpha_owner, TenantRole.TENANT_OWNER)
    bought = await http.post(f"{BILLING}/checkout", json={"plan_code": body["code"]})
    assert bought.status_code == 201, bought.text

    # The platform cannot assign it to Beta.
    version_id = result["version"]["id"]
    assigned = await http.post(
        f"{PLATFORM}/subscriptions/{beta_subscription.id}/change-plan",
        json={
            "plan_version_id": version_id,
            "mode": "next_renewal",
            "reason": "wrong company",
            "expected_revision": beta_subscription.revision,
        },
    )
    assert assigned.status_code == 422
    assert assigned.json()["error"]["code"] == CUSTOM_ERROR

    # And the table refuses it whoever writes it.
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE subscriptions SET scheduled_plan_version_id = :v WHERE id = :s"),
                {"v": uuid.UUID(version_id), "s": beta_subscription.id},
            )
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE plans SET tenant_id = :t WHERE id = :p"),
                {"t": beta.id, "p": uuid.UUID(result["plan"]["id"])},
            )


async def test_a_custom_plan_cannot_be_made_public_or_created_without_its_workspace(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    created = (
        await http.post(f"{PLATFORM}/tenants/{tenant.id}/custom-plan", json=_custom_body())
    ).json()
    plan = created["plan"]
    patched = await http.patch(
        f"{PLATFORM}/plans/{plan['id']}",
        json={"is_public": True, "expected_revision": plan["revision"], "reason": "publish it"},
    )
    assert patched.status_code == 422

    orphan = await http.post(
        f"{PLATFORM}/plans",
        json={
            "code": f"orphan-{uuid.uuid4().hex[:6]}",
            "name": "Orphan",
            "price": "10.00",
            "currency": "EGP",
            "interval": "monthly",
            "scope": "tenant",
            "reason": "no owner",
        },
    )
    assert orphan.status_code == 422
    missing = await http.post(f"{PLATFORM}/tenants/{uuid.uuid4()}/custom-plan", json=_custom_body())
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": "-1.00"},
        {"currency": "USD"},
        {"billing_interval": "weekly"},
        {"period_ai_turns": -1},
        {"storage_bytes": -5},
        {"whatsapp_numbers": -1},
        {"agents": 3},
    ],
)
async def test_invalid_custom_plan_terms_are_refused(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, overrides: dict[str, Any]
) -> None:
    """Spec 8: negative money or limits, another currency or interval, and any
    key outside the seven are refused before anything is written.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    response = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan", json=_custom_body(**overrides)
    )
    assert response.status_code == 422, (overrides, response.text)


async def test_a_limit_left_out_is_refused_and_null_is_unlimited(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 8: zero is none, null is unlimited, and neither happens by omission."""
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    body = _custom_body()
    del body["team_members"]
    omitted = await http.post(f"{PLATFORM}/tenants/{tenant.id}/custom-plan", json=body)
    assert omitted.status_code == 422

    explicit = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
        json=_custom_body(team_members=None, knowledge_documents=0),
    )
    assert explicit.status_code == 201, explicit.text
    limits = {row["key"]: row["limit"] for row in explicit.json()["version"]["limits"]}
    assert limits["team_members"] is None
    assert limits["knowledge_documents"] == 0


async def test_assigning_a_priced_custom_plan_now_needs_money_or_a_named_basis(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 11: never granted for free. Complimentary is said out loud; customer
    checkout assigns nothing until the customer pays.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    common = {
        "assign_to_tenant": True,
        "assignment_mode": "now",
        "expected_subscription_revision": subscription.revision,
    }
    bare = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan", json=_custom_body(**common)
    )
    assert bare.status_code == 422

    waiting = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
        json=_custom_body(**common, financial_basis="customer_checkout"),
    )
    assert waiting.status_code == 201, waiting.text
    assert waiting.json()["assignment"]["status"] == "awaiting_customer_checkout"
    await db_session.refresh(subscription)
    assert subscription.plan_id != uuid.UUID(waiting.json()["plan"]["id"])

    comp = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
        json=_custom_body(
            **{**common, "expected_subscription_revision": subscription.revision},
            financial_basis="complimentary",
        ),
    )
    assert comp.status_code == 201, comp.text
    assert comp.json()["assignment"]["status"] == "assigned"
    await db_session.refresh(subscription)
    assert subscription.plan_version_id == uuid.UUID(comp.json()["version"]["id"])
    assigned = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_CUSTOM_PLAN_ASSIGNED)
        .where(AuditLog.tenant_id == tenant.id)
    )
    assert assigned == 1

    stale = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
        json=_custom_body(
            **{**common, "expected_subscription_revision": 1}, financial_basis="complimentary"
        ),
    )
    assert stale.status_code == 409


async def test_a_new_custom_version_changes_nobody_until_they_are_moved(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 6, 70, CT-03: v2 is a new immutable version; the subscriber stays on
    v1; a stale publish is 409; the old version's terms are untouched.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    created = (
        await http.post(
            f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
            json=_custom_body(
                price="0.00",
                assign_to_tenant=True,
                assignment_mode="now",
                expected_subscription_revision=subscription.revision,
            ),
        )
    ).json()
    plan_id = created["plan"]["id"]
    v1 = uuid.UUID(created["version"]["id"])

    published = await http.post(
        f"{PLATFORM}/plans/{plan_id}/versions",
        json={
            "price": "1800.00",
            "currency": "EGP",
            "interval": "monthly",
            "limits": {"period_ai_turns": 60_000},
            "expected_version": 1,
            "reason": "Year two pricing.",
        },
    )
    assert published.status_code == 201, published.text
    stale = await http.post(
        f"{PLATFORM}/plans/{plan_id}/versions",
        json={
            "price": "1900.00",
            "currency": "EGP",
            "interval": "monthly",
            "limits": {},
            "expected_version": 1,
            "reason": "Stale view.",
        },
    )
    assert stale.status_code == 409

    await db_session.refresh(subscription)
    assert subscription.plan_version_id == v1
    original = await db_session.get(PlanVersion, v1)
    assert original is not None
    await db_session.refresh(original)
    assert original.price == Decimal("0.00")
    assert original.limits["period_ai_turns"] == 40_000
    version_audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_CUSTOM_PLAN_VERSION_CREATED)
        .where(AuditLog.target_id == uuid.UUID(plan_id))
    )
    assert version_audit is not None and version_audit.tenant_id == tenant.id

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE plan_versions SET price = 1 WHERE id = :v"), {"v": v1}
            )


async def test_a_retired_custom_plan_keeps_its_subscriber_and_sells_no_more(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 61: deactivation stops new checkouts and changes nobody holding it."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    created = (
        await http.post(
            f"{PLATFORM}/tenants/{tenant.id}/custom-plan",
            json=_custom_body(
                price="0.00",
                assign_to_tenant=True,
                assignment_mode="now",
                expected_subscription_revision=subscription.revision,
            ),
        )
    ).json()
    plan = created["plan"]
    retired = await http.post(
        f"{PLATFORM}/plans/{plan['id']}/deactivate",
        json={"expected_revision": plan["revision"], "reason": "Contract ended."},
    )
    assert retired.status_code == 200, retired.text
    await db_session.refresh(subscription)
    assert subscription.plan_id == uuid.UUID(plan["id"])
    _as_member(app, tenant, owner, TenantRole.MEMBER)
    assert plan["code"] not in {row["code"] for row in (await http.get(f"{BILLING}/plans")).json()}


# -------------------------------------------------------------- top-ups: API


async def test_topup_products_are_validated_versioned_and_listed(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 37-38, TU-13/TU-14: unknown keys and non-positive quantities are
    refused; a stale edit is 409; tenant products must name their workspace.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    good = {
        "code": f"storage-{uuid.uuid4().hex[:6]}",
        "name": "Storage +25 GiB",
        "entitlement_key": "storage_bytes",
        "quantity": 25 * GIB,
        "price": "100.00",
        "currency": "EGP",
        "reason": "Catalogue.",
    }
    for bad in (
        {"entitlement_key": "agents"},
        {"entitlement_key": "owned_workspaces"},
        {"entitlement_key": "made_up"},
        {"quantity": 0},
        {"quantity": -5},
        {"price": "-1.00"},
        {"currency": "USD"},
        {"scope": "tenant"},
        {"tenant_id": str(tenant.id)},
    ):
        refused = await http.post(f"{PLATFORM}/topups", json={**good, **bad})
        assert refused.status_code == 422, (bad, refused.text)

    created = await http.post(f"{PLATFORM}/topups", json=good)
    assert created.status_code == 201, created.text
    row = created.json()
    assert (row["quantity"], row["kind"], row["enforced"]) == (25 * GIB, "capacity", True)
    duplicate = await http.post(f"{PLATFORM}/topups", json=good)
    assert duplicate.status_code == 409

    edited = await http.patch(
        f"{PLATFORM}/topups/{row['id']}",
        json={"price": "90.00", "expected_revision": row["revision"], "reason": "Promo."},
    )
    assert edited.status_code == 200, edited.text
    stale = await http.patch(
        f"{PLATFORM}/topups/{row['id']}",
        json={"price": "80.00", "expected_revision": row["revision"], "reason": "Stale."},
    )
    assert stale.status_code == 409
    deactivated = await http.post(
        f"{PLATFORM}/topups/{row['id']}/deactivate",
        json={"expected_revision": edited.json()["revision"], "reason": "Pause."},
    )
    assert deactivated.status_code == 200 and deactivated.json()["is_active"] is False
    listed = await http.get(f"{PLATFORM}/topups", params={"entitlement_key": "storage_bytes"})
    assert row["id"] in {item["id"] for item in listed.json()["items"]}

    for_tenant = await http.post(
        f"{PLATFORM}/topups",
        json={
            **good,
            "code": f"abc-ai-{uuid.uuid4().hex[:6]}",
            "entitlement_key": "period_ai_turns",
            "quantity": 50_000,
            "scope": "tenant",
            "tenant_id": str(tenant.id),
        },
    )
    assert for_tenant.status_code == 201, for_tenant.text
    assert for_tenant.json()["tenant_id"] == str(tenant.id)


async def test_a_workspace_buys_a_topup_through_its_own_checkout(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, intentions: Intentions
) -> None:
    """Spec 23, 35-36, 57: members browse, only owners buy, one key one page,
    and nothing in any response can charge a card or reprice a purchase.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    other, other_owner, _ = await workspace(db_session, now=now, name="Other")
    shared = await product(
        db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=10_000
    )
    theirs = await product(
        db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=50_000, tenant=other
    )

    _as_member(app, tenant, owner, TenantRole.MEMBER)
    listed = await http.get(f"{BILLING}/topups", params={"entitlement_key": "period_ai_turns"})
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()}
    assert str(shared.id) in ids and str(theirs.id) not in ids
    assert (await http.post(f"{BILLING}/topups/{shared.id}/checkout", json={})).status_code == 403

    _as_member(app, tenant, owner, TenantRole.TENANT_OWNER)
    priced = await http.post(f"{BILLING}/topups/{shared.id}/checkout", json={"price": "1.00"})
    assert priced.status_code == 422, "a client cannot name a price"
    foreign = await http.post(f"{BILLING}/topups/{theirs.id}/checkout", json={})
    missing = await http.post(f"{BILLING}/topups/{uuid.uuid4()}/checkout", json={})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]

    started = await http.post(
        f"{BILLING}/topups/{shared.id}/checkout", json={"idempotency_key": "req-1"}
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert (body["amount"], body["currency"], body["quantity"]) == ("200.00", "EGP", 10_000)
    assert "redirect_url" in body
    assert "client_secret" not in body
    again = await http.post(
        f"{BILLING}/topups/{shared.id}/checkout", json={"idempotency_key": "req-1"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["details"]["purchase_id"] == body["purchase_id"]
    assert intentions.count == 1

    history = await http.get(f"{BILLING}/topup-purchases")
    assert history.status_code == 200
    page = history.json()
    assert page["total"] == 1
    assert page["items"][0]["status"] == "pending"
    assert page["items"][0]["payment_status"] == "pending"
    _clean(page)

    summary = await http.get(f"{BILLING}/summary")
    assert summary.status_code == 200, summary.text
    state = summary.json()
    assert {row["key"] for row in state["entitlements"]} == {key.value for key in TopupEntitlement}
    ai = next(row for row in state["entitlements"] if row["key"] == "period_ai_turns")
    assert (ai["base_limit"], ai["topup_limit"], ai["effective_limit"]) == (5_000, 0, 5_000)
    messages = next(row for row in state["entitlements"] if row["key"] == "period_messages")
    assert messages["enforced"] is False
    assert state["topups_available"] == 1
    _clean(state)

    # Another workspace cannot read this one's purchases.
    _as_member(app, other, other_owner, TenantRole.TENANT_OWNER)
    assert (await http.get(f"{BILLING}/topup-purchases")).json()["total"] == 0


async def test_every_member_sees_the_limit_breakdown(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 34: base, top-ups, grants, effective, used, remaining, over_limit."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    _as_member(app, tenant, owner, TenantRole.MEMBER)
    rows = {row["key"]: row for row in (await http.get(f"{BILLING}/entitlements")).json()}
    team = rows["team_members"]
    assert team["kind"] == "capacity"
    assert (team["base_limit"], team["effective_limit"], team["limit"]) == (10, 10, 10)
    assert (team["used"], team["remaining"], team["over_limit"]) == (1, 9, False)
    assert team["period_start"] is None
    turns = rows["period_ai_turns"]
    assert turns["kind"] == "usage" and turns["period_end"] is not None


async def test_the_platform_company_summary_is_one_complete_read(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Spec 39, 85: plan, version, custom flag, period, renewal, entitlements
    with grants, active top-ups, recent money - and the read is audited.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    staff = await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    granted = await http.post(
        f"{PLATFORM}/tenants/{tenant.id}/topups/grant",
        json={
            "entitlement_key": "team_members",
            "quantity": 5,
            "reason": "Onboarding help.",
            "expected_subscription_revision": subscription.revision,
        },
    )
    assert granted.status_code == 201, granted.text
    assert (granted.json()["source"], granted.json()["invoice_id"]) == ("platform_grant", None)

    response = await http.get(f"{PLATFORM}/tenants/{tenant.id}/summary")
    assert response.status_code == 200, response.text
    summary = response.json()
    _clean(summary)
    assert summary["tenant"]["id"] == str(tenant.id)
    assert summary["plan"]["code"] == "pro" and summary["custom_plan"] is False
    # The version the subscription is pinned to - 1 on a model-built schema,
    # 2 on a migration-built one, where `pro` is seeded before the fixture
    # publishes its own terms.
    pinned = await db_session.get(PlanVersion, subscription.plan_version_id)
    assert pinned is not None
    assert summary["plan_version"]["id"] == str(pinned.id)
    assert summary["plan_version"]["version"] == pinned.version
    assert summary["price"] == "99.00"
    assert summary["next_renewal_at"] is not None
    team = next(row for row in summary["entitlements"] if row["key"] == "team_members")
    assert (team["base_limit"], team["platform_grant_limit"], team["effective_limit"]) == (
        10,
        5,
        15,
    )
    assert len(summary["active_topups"]) == 1
    assert summary["active_topups"][0]["source"] == "platform_grant"
    read = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.PLATFORM_BILLING_READ)
        .where(AuditLog.actor_id == staff.id)
        .order_by(AuditLog.occurred_at.desc())
    )
    assert read is not None
    assert (await http.get(f"{PLATFORM}/tenants/{uuid.uuid4()}/summary")).status_code == 404


async def test_a_refund_review_is_only_for_a_refunded_grant(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    granted = (
        await http.post(
            f"{PLATFORM}/tenants/{tenant.id}/topups/grant",
            json={
                "entitlement_key": "knowledge_documents",
                "quantity": 100,
                "reason": "Migration from another tool.",
                "expected_subscription_revision": subscription.revision,
            },
        )
    ).json()
    review = await http.post(
        f"{PLATFORM}/topup-purchases/{granted['id']}/refund-review",
        json={"decision": "withdraw", "reason": "no", "expected_revision": granted["revision"]},
    )
    assert review.status_code == 422  # reason too short
    review = await http.post(
        f"{PLATFORM}/topup-purchases/{granted['id']}/refund-review",
        json={
            "decision": "withdraw",
            "reason": "Not refunded.",
            "expected_revision": granted["revision"],
        },
    )
    assert review.status_code == 409

    listed = await http.get(
        f"{PLATFORM}/topup-purchases",
        params={"tenant_id": str(tenant.id), "source": "platform_grant"},
    )
    assert listed.status_code == 200 and listed.json()["total"] == 1


async def test_the_plan_list_filters_by_scope_and_workspace(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, _ = await workspace(db_session, now=now)
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)
    created = (
        await http.post(f"{PLATFORM}/tenants/{tenant.id}/custom-plan", json=_custom_body())
    ).json()
    listed = await http.get(
        f"{PLATFORM}/plans", params={"scope": PlanScope.TENANT.value, "tenant_id": str(tenant.id)}
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [created["plan"]["id"]]
    public = await http.get(f"{PLATFORM}/plans", params={"scope": "public"})
    assert created["plan"]["id"] not in {row["id"] for row in public.json()["items"]}
