"""The invoice routes, and the line between a workspace and the platform.

The line is the point of this file. A workspace reads its own invoices; only
platform staff record a payment or void a bill, because a customer able to mark
their own invoice paid is a customer who pays nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_current_user,
    get_invoice_service,
    get_platform_billing_service,
)
from app.core.exceptions import ConflictError, TenantIsolationError
from app.db.models import Membership, PlatformRole, Tenant, TenantRole, TenantStatus, User
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus

pytestmark = pytest.mark.integration

PATH = "/api/v1/invoices"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
INVOICE_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
PERIOD_START = datetime(2026, 7, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _invoice(**overrides) -> Invoice:
    values = {
        "id": INVOICE_ID,
        "tenant_id": TENANT_ID,
        "subscription_id": None,
        "status": InvoiceStatus.OPEN,
        "plan_code": "pro",
        "amount_due": Decimal("99.00"),
        "amount_paid": Decimal("0.00"),
        "currency": "USD",
        "period_start": PERIOD_START,
        "period_end": NOW,
        "issued_at": NOW,
        "paid_at": None,
        "lines": [
            {"kind": "subscription", "description": "Pro plan", "quantity": 1, "amount": "99.00"},
            {
                "kind": "usage",
                "description": "whatsapp_message_sent",
                "quantity": 120,
                "unit": "count",
                "amount": "0.00",
            },
        ],
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Invoice(**values)


class StubInvoices:
    """A workspace's own invoices."""

    def __init__(self) -> None:
        self.missing = False
        self.invoice = _invoice()

    def _guard(self) -> None:
        if self.missing:
            raise TenantIsolationError()

    async def list_invoices(self, *, limit: int = 50):
        return [self.invoice]

    async def get(self, invoice_id):
        self._guard()
        return self.invoice

    async def payments_for(self, invoice_id):
        self._guard()
        return [
            Payment(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                invoice_id=invoice_id,
                status=PaymentStatus.FAILED,
                amount=Decimal("99.00"),
                currency="USD",
                provider="card",
                failure_reason="The card was declined.",
                processed_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        ]


class StubPlatformBilling:
    """The platform's side: recording money and withdrawing bills."""

    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.voided: list[uuid.UUID] = []
        self.conflict = False

    async def record_payment(self, *, invoice_id, amount, provider, reference=None):
        self.recorded.append({"invoice_id": invoice_id, "amount": amount, "provider": provider})
        return Payment(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            invoice_id=invoice_id,
            status=PaymentStatus.SUCCEEDED,
            amount=amount,
            currency="USD",
            provider=provider,
            provider_reference=reference,
            processed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )

    async def void(self, invoice_id, *, reason=None):
        if self.conflict:
            raise ConflictError("A paid invoice cannot be voided. Refund it instead.")
        self.voided.append(invoice_id)
        return _invoice(status=InvoiceStatus.VOID)


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(id=uuid.uuid4(), user_id=USER_ID, tenant_id=TENANT_ID, role=role),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


def _as(app, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


def _as_platform(app, platform_role: PlatformRole | None) -> None:
    from app.api.dependencies import CurrentUser
    from app.core.security import TokenClaims, TokenType

    user = User(
        id=uuid.uuid4(),
        email="staff@example.com",
        is_active=True,
        platform_role=platform_role,
    )
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        tenant_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=user, claims=claims)


@pytest.fixture
def invoices(app) -> StubInvoices:
    stub = StubInvoices()
    app.dependency_overrides[get_invoice_service] = lambda: stub
    return stub


@pytest.fixture
def platform_billing(app) -> StubPlatformBilling:
    stub = StubPlatformBilling()
    app.dependency_overrides[get_platform_billing_service] = lambda: stub
    return stub


# ----------------------------------------------------------------- reading


async def test_an_owner_can_read_their_invoices(client, app, invoices):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.get(PATH)

    assert response.status_code == 200
    invoice = response.json()[0]
    assert invoice["plan_code"] == "pro"
    # Amounts are strings: a JSON number is a double in most clients, and 19.99
    # does not survive that trip intact.
    assert invoice["amount_due"] == "99.00"
    assert invoice["outstanding"] == "99.00"


async def test_the_lines_are_returned_as_they_were_written(client, app, invoices):
    _as(app, TenantRole.TENANT_OWNER)

    lines = (await client.get(f"{PATH}/{INVOICE_ID}")).json()["lines"]

    assert lines[0]["kind"] == "subscription"
    assert lines[1]["quantity"] == 120
    # Present at zero rather than omitted: no per-unit price is stored anywhere,
    # and the shape of a line should not change on the day one is.
    assert lines[1]["amount"] == "0.00"


async def test_a_member_cannot_read_invoices(client, app, invoices):
    """What the company spends is not something everyone staffing an inbox is
    entitled to see."""
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH)

    assert response.status_code == 403


async def test_another_workspaces_invoice_is_not_found(client, app, invoices):
    _as(app, TenantRole.TENANT_OWNER)
    invoices.missing = True

    response = await client.get(f"{PATH}/{INVOICE_ID}")

    assert response.status_code == 404


async def test_failed_attempts_are_shown_to_the_customer(client, app, invoices):
    """Somebody whose card was declined twice should see that without asking."""
    _as(app, TenantRole.TENANT_OWNER)

    payments = (await client.get(f"{PATH}/{INVOICE_ID}/payments")).json()

    assert payments[0]["status"] == "failed"
    assert payments[0]["failure_reason"] == "The card was declined."


# --------------------------------------------------------------- platform


async def test_platform_staff_can_record_money_that_arrived(client, app, platform_billing):
    _as_platform(app, PlatformRole.PLATFORM_ADMIN)

    response = await client.post(
        f"{PATH}/{INVOICE_ID}/payments",
        json={"amount": "99.00", "provider": "manual", "reference": "transfer-1"},
    )

    assert response.status_code == 200
    assert platform_billing.recorded[0]["amount"] == Decimal("99.00")
    assert response.json()["status"] == "succeeded"


async def test_a_workspace_owner_cannot_mark_their_own_invoice_paid(
    client,
    app,
    platform_billing,
):
    """The whole reason this is a platform route: a customer who can do this
    pays nothing."""
    _as_platform(app, None)

    response = await client.post(
        f"{PATH}/{INVOICE_ID}/payments",
        json={"amount": "99.00", "provider": "manual"},
    )

    assert response.status_code == 403
    assert platform_billing.recorded == []


async def test_a_payment_for_nothing_is_rejected_before_the_service(
    client,
    app,
    platform_billing,
):
    _as_platform(app, PlatformRole.PLATFORM_OWNER)

    response = await client.post(
        f"{PATH}/{INVOICE_ID}/payments",
        json={"amount": "0.00", "provider": "manual"},
    )

    assert response.status_code == 422
    assert platform_billing.recorded == []


async def test_platform_staff_can_withdraw_an_invoice(client, app, platform_billing):
    _as_platform(app, PlatformRole.PLATFORM_OWNER)

    response = await client.post(f"{PATH}/{INVOICE_ID}/void", json={"reason": "Issued in error."})

    assert response.status_code == 200
    assert response.json()["status"] == "void"
    assert platform_billing.voided == [INVOICE_ID]


async def test_voiding_a_paid_invoice_conflicts(client, app, platform_billing):
    """That is a refund, and a different conversation."""
    _as_platform(app, PlatformRole.PLATFORM_OWNER)
    platform_billing.conflict = True

    response = await client.post(f"{PATH}/{INVOICE_ID}/void", json={})

    assert response.status_code == 409


async def test_a_workspace_cannot_void_its_own_bill(client, app, platform_billing):
    _as_platform(app, None)

    response = await client.post(f"{PATH}/{INVOICE_ID}/void", json={})

    assert response.status_code == 403
    assert platform_billing.voided == []
