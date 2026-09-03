"""Who may do what with money, over HTTP, against real rows.

Three questions asked of every billing endpoint that this phase added or
changed, and each has been the shape of a real vulnerability somewhere:

**Does it take the right role?** Choosing a plan, starting a checkout and
issuing a refund all commit the company to something. An administrator who can
invite colleagues is not thereby somebody who can spend the company's money,
and the split is enforced by the route rather than by a client hiding a button.

**Is it scoped to the workspace?** Every identifier in this subsystem is a UUID
that appears in a URL, an email or a provider's dashboard, and any of them can
leak. So naming another workspace's payment or invoice must answer not-found -
not forbidden, which would confirm the id is real, and certainly not the
resource.

**Does it leak a credential?** The provider's client secret is a bearer value
for one payment page. It travels inside the redirect URL because the customer's
browser has to carry it, and it must appear nowhere else: not in a field of its
own, not in the payment a client later polls, not in a log line.

The whole application is built, so the real dependency graph, the real
authorization dependencies and the real serialisers run. Only the session and
the workspace resolution are substituted.
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
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_entitlement_service,
)
from app.core.config import Settings
from app.core.dependencies import get_session
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.billing import BillingInterval, LimitKey, Plan
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.integrations.billing import paymob
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

BILLING = "/api/v1/billing"
HMAC_SECRET = "a-test-hmac-secret"
CLIENT_SECRET = "csk_test_thisisabearertokenforonepage"
SECRET_KEY = "sk_test_notreal000000"


class _Infra:
    """Stands in for the database and Redis clients on application state."""

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

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
        "billing_provider": "paymob",
        "paymob_secret_key": SECRET_KEY,
        "paymob_public_key": "pk_test_notreal000000",
        "paymob_hmac_secret": HMAC_SECRET,
        "paymob_integration_ids": [4097558],
        "app_public_url": "https://app.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def provider_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every provider call without a socket.

    Patched onto the provider class rather than injected, because the point of
    this file is that the *real* dependency graph runs - and that graph builds
    its own provider from settings.
    """
    original = paymob.PaymobProvider.__init__

    def patched(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault(
            "transport",
            httpx.MockTransport(
                lambda request: (
                    httpx.Response(
                        201,
                        json={"id": "pi_test_1", "client_secret": CLIENT_SECRET},
                    )
                    if "intention" in str(request.url)
                    else httpx.Response(200, json={"id": 579305, "success": True})
                ),
            ),
        )
        original(self, **kwargs)

    monkeypatch.setattr(paymob.PaymobProvider, "__init__", patched)


@pytest.fixture
def app(db_session: AsyncSession, provider_transport: None) -> Iterator[FastAPI]:
    application = create_app(_settings())
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


async def _workspace_rows(session: AsyncSession, slug: str) -> tuple[Tenant, User]:
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    user = User(
        email=f"{slug}-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Owner Person",
        hashed_password="x",
        is_active=True,
    )
    session.add_all([tenant, user])
    await session.flush()
    return tenant, user


def _act_as(app: FastAPI, tenant: Tenant, user: User, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=user,
        membership=Membership(
            id=uuid.uuid4(),
            user_id=user.id,
            tenant_id=tenant.id,
            role=role,
            status=TenantStatus.ACTIVE,
        ),
        tenant=tenant,
    )


async def _plan(session: AsyncSession, *, is_public: bool = True) -> Plan:
    plan = Plan(
        code=f"pro-{uuid.uuid4().hex[:6]}",
        name="Pro",
        price=Decimal("99.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        is_public=is_public,
        limits={LimitKey.AGENTS.value: 5},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _collected(session: AsyncSession, tenant: Tenant) -> tuple[Invoice, Payment]:
    now = datetime.now(UTC)
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.PAID,
        plan_code="pro",
        amount_due=Decimal("99.00"),
        amount_paid=Decimal("99.00"),
        currency="EGP",
        period_start=now,
        period_end=now + timedelta(days=30),
        lines=[],
        paid_at=now,
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.SUCCEEDED,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
        provider_reference="192036465",
        refunded_amount=Decimal("0.00"),
        processed_at=now,
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


# ------------------------------------------------------------------ the roles


@pytest.mark.parametrize("role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN])
async def test_only_an_owner_may_start_a_checkout(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession, role: TenantRole
) -> None:
    """An administrator runs the workspace; an owner spends its money.

    Somebody who can invite colleagues and configure agents is not thereby
    somebody who can commit the company to a bill, and this is the same line
    `POST /billing/subscription` already draws.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _act_as(app, tenant, user, role)

    response = await http.post(f"{BILLING}/checkout", json={"plan_code": plan.code})

    assert response.status_code == 403


@pytest.mark.parametrize("role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN])
async def test_only_an_owner_may_refund(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession, role: TenantRole
) -> None:
    """The one action that moves money *out* takes the highest role there is."""
    tenant, user = await _workspace_rows(db_session, "acme")
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, role)

    response = await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})

    assert response.status_code == 403


@pytest.mark.parametrize("role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN])
async def test_only_an_owner_may_read_a_payment(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession, role: TenantRole
) -> None:
    """What the company paid is not something every colleague may read.

    The same line `GET /invoices` draws, and drawn here too because a payment
    carries the amount, the provider and the card's last four digits.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, role)

    response = await http.get(f"{BILLING}/payments/{payment.id}")

    assert response.status_code == 403


async def test_an_owner_may_do_all_three(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """The other half: the restriction is a role check, not a broken route."""
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    assert (await http.get(f"{BILLING}/payments/{payment.id}")).status_code == 200
    assert (await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})).status_code == 202
    assert (
        await http.post(f"{BILLING}/checkout", json={"plan_code": plan.code})
    ).status_code == 201


# ------------------------------------------------------------- the boundaries


async def test_another_workspaces_payment_is_not_found_rather_than_forbidden(
    http: AsyncClient,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """404, deliberately, and it is the difference that matters.

    A 403 would confirm the id names a real payment - which is exactly what
    somebody holding a leaked reference wants to learn. Not-found is what an
    invented id gets, so the two are indistinguishable.
    """
    acme, acme_user = await _workspace_rows(db_session, "acme")
    globex, _ = await _workspace_rows(db_session, "globex")
    _, payment = await _collected(db_session, globex)
    _act_as(app, acme, acme_user, TenantRole.TENANT_OWNER)

    read = await http.get(f"{BILLING}/payments/{payment.id}")
    refund = await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})

    assert read.status_code == 404
    assert refund.status_code == 404
    assert str(payment.id) not in read.text


async def test_a_checkout_cannot_be_started_against_another_workspaces_invoice(
    http: AsyncClient,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """Paying somebody else's bill is not generosity, it is a way in.

    A settled invoice moves that workspace's subscription out of `past_due`, so
    an attacker who could collect against another company's invoice could
    control its billing state.
    """
    acme, acme_user = await _workspace_rows(db_session, "acme")
    globex, _ = await _workspace_rows(db_session, "globex")
    invoice, _ = await _collected(db_session, globex)
    invoice.status = InvoiceStatus.OPEN
    invoice.amount_paid = Decimal("0.00")
    await db_session.flush()
    _act_as(app, acme, acme_user, TenantRole.TENANT_OWNER)

    response = await http.post(f"{BILLING}/checkout", json={"invoice_id": str(invoice.id)})

    assert response.status_code == 404


async def test_a_private_plan_cannot_be_bought_by_naming_its_code(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """The catalogue filter is a display rule; this is the authorization rule.

    `GET /billing/plans` hides a bespoke plan, and hiding is not preventing.
    The refusal is deliberately the same as for a plan that does not exist, so
    guessing codes teaches nobody which ones are real.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    private = await _plan(db_session, is_public=False)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    refused = await http.post(f"{BILLING}/checkout", json={"plan_code": private.code})
    invented = await http.post(f"{BILLING}/checkout", json={"plan_code": "no-such-plan"})

    assert refused.status_code == invented.status_code == 422

    # Compared without the request id, which is per-request by design and is
    # the only thing that legitimately differs between the two answers.
    def _said(response: Response) -> tuple[str, str]:
        error = response.json()["error"]
        return error["code"], error["message"]

    assert _said(refused) == _said(invented)


# ------------------------------------------------------------ what comes back


async def test_a_client_cannot_ask_to_be_charged_a_figure_of_its_choosing(
    http: AsyncClient,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """`extra="forbid"`, so trying is a 422 rather than a field ignored.

    A quietly-ignored `amount` is indistinguishable from a working one to
    whoever is trying it, and the day somebody wires it up is the day it works.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    response = await http.post(
        f"{BILLING}/checkout",
        json={"plan_code": plan.code, "amount": "0.01", "currency": "USD"},
    )

    assert response.status_code == 422


async def test_a_checkout_must_name_exactly_one_thing_to_pay_for(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    tenant, user = await _workspace_rows(db_session, "acme")
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    neither = await http.post(f"{BILLING}/checkout", json={})
    both = await http.post(
        f"{BILLING}/checkout",
        json={"plan_code": "pro", "invoice_id": str(uuid.uuid4())},
    )

    assert neither.status_code == 422
    assert both.status_code == 422


async def test_the_client_secret_travels_only_inside_the_redirect_url(
    http: AsyncClient,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """It is a bearer token for one payment page.

    The browser has to carry it, so it is in the URL. A field of its own would
    invite a client to store it, and the payment a client polls afterwards must
    not carry it at all - that response is fetched repeatedly, cached and
    logged.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    started = (await http.post(f"{BILLING}/checkout", json={"plan_code": plan.code})).json()
    polled = await http.get(f"{BILLING}/payments/{started['payment_id']}")

    assert CLIENT_SECRET in started["redirect_url"]
    assert not any(
        CLIENT_SECRET in str(value) for key, value in started.items() if key != "redirect_url"
    )
    assert CLIENT_SECRET not in polled.text


async def test_no_response_carries_a_credential_of_ours(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """The secret key and the HMAC secret belong to the deployment, not a page.

    Either one in a response body would let any authenticated customer create
    charges or forge callbacks for every other customer.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    bodies = [
        (await http.post(f"{BILLING}/checkout", json={"plan_code": plan.code})).text,
        (await http.get(f"{BILLING}/payments/{payment.id}")).text,
        (await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})).text,
    ]

    for body in bodies:
        assert SECRET_KEY not in body
        assert HMAC_SECRET not in body


async def test_a_polled_payment_reports_pending_rather_than_pretending(
    http: AsyncClient,
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    """The state a client must not read as failure.

    3-D Secure and several local methods complete after the customer has
    already been sent back, so a client that treats `pending` as "it did not
    work" tells people their payment failed while it is still working.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    plan = await _plan(db_session)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    started = (await http.post(f"{BILLING}/checkout", json={"plan_code": plan.code})).json()
    polled = (await http.get(f"{BILLING}/payments/{started['payment_id']}")).json()

    assert polled["status"] == "pending"
    assert polled["processed_at"] is None
    assert polled["invoice_id"] == started["invoice_id"]


async def test_a_requested_refund_says_pending_rather_than_refunded(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """202 and `refund_pending`, because the money has not moved yet.

    A client rendering "refunded" from this response would tell a customer
    their money is back before the provider has confirmed anything.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    _, payment = await _collected(db_session, tenant)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    response = await http.post(f"{BILLING}/payments/{payment.id}/refund", json={})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["refund_pending"] is True
    assert body["refunded_amount"] == "0.00"


# ---------------------------------------------------------- saved cards


async def _saved_card(session: AsyncSession, tenant: Tenant, *, token: str) -> PaymentMethod:
    method = PaymentMethod(
        tenant_id=tenant.id,
        provider="paymob",
        provider_token=token,
        provider_token_id="15978654",
        masked_pan="xxxx-xxxx-xxxx-2346",
        brand="MasterCard",
        status=PaymentMethodStatus.ACTIVE,
        is_default=True,
    )
    session.add(method)
    await session.flush()
    return method


@pytest.mark.parametrize("role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN])
async def test_only_an_owner_may_read_saved_cards(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession, role: TenantRole
) -> None:
    """Which card the company pays with is not everyone's business.

    The same line invoices draw, and drawn here because a saved card names a
    scheme and the last four digits.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    await _saved_card(db_session, tenant, token=f"tok-{uuid.uuid4().hex[:8]}")
    _act_as(app, tenant, user, role)

    response = await http.get(f"{BILLING}/payment-methods")

    assert response.status_code == 403


@pytest.mark.parametrize("role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN])
async def test_only_an_owner_may_change_which_card_renewals_use(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession, role: TenantRole
) -> None:
    """Pointing automatic renewals at a different card is a billing decision."""
    tenant, user = await _workspace_rows(db_session, "acme")
    method = await _saved_card(db_session, tenant, token=f"tok-{uuid.uuid4().hex[:8]}")
    _act_as(app, tenant, user, role)

    default = await http.post(f"{BILLING}/payment-methods/{method.id}/default")
    removed = await http.delete(f"{BILLING}/payment-methods/{method.id}")

    assert default.status_code == 403
    assert removed.status_code == 403


async def test_an_owner_may_manage_saved_cards(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """The other half: the restriction is a role check, not a broken route."""
    tenant, user = await _workspace_rows(db_session, "acme")
    method = await _saved_card(db_session, tenant, token=f"tok-{uuid.uuid4().hex[:8]}")
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    listed = await http.get(f"{BILLING}/payment-methods")
    default = await http.post(f"{BILLING}/payment-methods/{method.id}/default")
    removed = await http.delete(f"{BILLING}/payment-methods/{method.id}")

    assert listed.status_code == 200
    assert listed.json()[0]["masked_pan"] == "xxxx-xxxx-xxxx-2346"
    assert default.status_code == 200
    assert removed.status_code == 200
    assert removed.json()["is_default"] is False


async def test_another_workspaces_card_is_not_found(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """404 on an object that can be charged, which is where it matters most."""
    acme, acme_user = await _workspace_rows(db_session, "acme")
    globex, _ = await _workspace_rows(db_session, "globex")
    method = await _saved_card(db_session, globex, token=f"tok-{uuid.uuid4().hex[:8]}")
    _act_as(app, acme, acme_user, TenantRole.TENANT_OWNER)

    default = await http.post(f"{BILLING}/payment-methods/{method.id}/default")
    removed = await http.delete(f"{BILLING}/payment-methods/{method.id}")
    listed = await http.get(f"{BILLING}/payment-methods")

    assert default.status_code == 404
    assert removed.status_code == 404
    assert listed.json() == [], "another workspace's card is invisible, not merely unusable"


async def test_the_card_token_never_leaves_through_the_api(
    http: AsyncClient, app: FastAPI, db_session: AsyncSession
) -> None:
    """It is what charges the card, and a client has no use for it.

    A response carrying it would be one more place it could be logged, cached
    or copied into a bug report.
    """
    tenant, user = await _workspace_rows(db_session, "acme")
    token = f"tok-secret-{uuid.uuid4().hex[:8]}"
    method = await _saved_card(db_session, tenant, token=token)
    _act_as(app, tenant, user, TenantRole.TENANT_OWNER)

    bodies = [
        (await http.get(f"{BILLING}/payment-methods")).text,
        (await http.post(f"{BILLING}/payment-methods/{method.id}/default")).text,
        (await http.delete(f"{BILLING}/payment-methods/{method.id}")).text,
    ]

    for body in bodies:
        assert token not in body
