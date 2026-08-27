"""The payment callback endpoint, driven over HTTP.

What the service tests cannot see: whether the route is reachable without a
credential (it must be), whether an unsigned request can reach the database (it
must not), what a caller learns from the response (nothing), and whether the
endpoint refuses or silently accepts when no provider is configured.

The response shape is itself a security property here and is tested as one.
Applied, duplicate, unmatched and mismatched all answer the same 200 body,
because a reply that distinguished them would tell a caller which payment
references exist - and by that point the caller has proven only that they hold
a signing secret for *some* payload, not that they own anything.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.db.models.billing import BillingInterval, LimitKey, Plan
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import hmac_signature
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

WEBHOOK = "/api/v1/webhooks/paymob"
HMAC_SECRET = "a-test-hmac-secret"


class _Infra:
    """Stands in for the database and Redis clients on application state."""

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
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
        "billing_provider": "paymob",
        "paymob_secret_key": "sk_test_notreal",
        "paymob_public_key": "pk_test_notreal",
        "paymob_hmac_secret": HMAC_SECRET,
        "paymob_integration_ids": [4097558],
        "app_public_url": "https://app.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def webhook_settings() -> Settings:
    return _settings()


@pytest.fixture
def app(webhook_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(webhook_settings)
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


async def _paid_checkout(session: AsyncSession) -> tuple[Invoice, Payment]:
    """A workspace with an open invoice and a pending payment against it."""
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    plan = Plan(
        code=f"pro-{uuid.uuid4().hex[:6]}",
        name="Pro",
        price=Decimal("99.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.AGENTS.value: 5},
    )
    session.add(plan)
    await session.flush()

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=None,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=now,
        period_end=now + timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()

    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


def _transaction(*, reference: str | None, amount_cents: int = 9900, **overrides) -> dict:
    transaction = {
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


def _body(transaction: dict) -> tuple[dict, str]:
    return (
        {"type": "TRANSACTION", "obj": transaction},
        hmac_signature(transaction, secret=HMAC_SECRET),
    )


# ------------------------------------------------------------- the happy path


async def test_a_signed_callback_settles_the_invoice(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    invoice, payment = await _paid_checkout(db_session)
    body, signature = _body(_transaction(reference=str(payment.id)))

    response = await http.post(WEBHOOK, json=body, params={"hmac": signature})

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    await db_session.flush()
    assert invoice.status is InvoiceStatus.PAID


# ------------------------------------------------------------- verification


async def test_an_unsigned_callback_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """No signature, no processing. The endpoint has no other credential."""
    invoice, payment = await _paid_checkout(db_session)
    body, _ = _body(_transaction(reference=str(payment.id)))

    response = await http.post(WEBHOOK, json=body)

    assert response.status_code == 403
    await db_session.flush()
    assert invoice.status is InvoiceStatus.OPEN


async def test_a_forged_callback_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The attack this endpoint exists to survive: a stranger claiming payment.

    The body names a real payment and says it succeeded. Only the signature is
    wrong, and that is enough.
    """
    invoice, payment = await _paid_checkout(db_session)
    body, _ = _body(_transaction(reference=str(payment.id)))

    response = await http.post(WEBHOOK, json=body, params={"hmac": "0" * 128})

    assert response.status_code == 403
    await db_session.flush()
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")


async def test_a_tampered_amount_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Signed for one amount, delivered claiming another."""
    invoice, payment = await _paid_checkout(db_session)
    transaction = _transaction(reference=str(payment.id))
    body, signature = _body(transaction)
    body["obj"]["amount_cents"] = 1

    response = await http.post(WEBHOOK, json=body, params={"hmac": signature})

    assert response.status_code == 403
    await db_session.flush()
    assert invoice.status is InvoiceStatus.OPEN


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "TRANSACTION"},
        {"type": "TRANSACTION", "obj": None},
        {"type": "TRANSACTION", "obj": "text"},
        {"obj": {"id": 1}},
        [],
    ],
)
async def test_a_malformed_body_is_refused(http: AsyncClient, payload: object) -> None:
    response = await http.post(WEBHOOK, json=payload, params={"hmac": "0" * 128})

    assert response.status_code == 403


async def test_a_non_json_body_is_refused(http: AsyncClient) -> None:
    response = await http.post(
        WEBHOOK,
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
        params={"hmac": "0" * 128},
    )

    assert response.status_code == 403


async def test_a_deployment_without_a_provider_refuses_rather_than_drops(
    db_session: AsyncSession,
) -> None:
    """503, so the provider retries.

    Answering 200 would tell Paymob the payment was recorded when nothing was,
    and it would never be sent again - the worst outcome this subsystem has,
    because the customer has been charged.
    """
    settings = _settings(
        billing_provider="manual",
        paymob_secret_key=None,
        paymob_public_key=None,
        paymob_hmac_secret=None,
        paymob_integration_ids=[],
    )
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        response = await client.post(WEBHOOK, json={}, params={"hmac": "x"})

    application.dependency_overrides.clear()
    assert response.status_code == 503


# ---------------------------------------------------------- what it discloses


async def test_every_processed_outcome_answers_identically(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Applied, duplicate, unmatched and mismatched are one response.

    A reply that distinguished them would confirm which payment references
    exist to somebody who has proven only that they can sign a payload.
    """
    invoice, payment = await _paid_checkout(db_session)

    applied_body, applied_sig = _body(_transaction(reference=str(payment.id)))
    applied = await http.post(WEBHOOK, json=applied_body, params={"hmac": applied_sig})
    duplicate = await http.post(WEBHOOK, json=applied_body, params={"hmac": applied_sig})

    unknown_body, unknown_sig = _body(_transaction(reference=str(uuid.uuid4())))
    unmatched = await http.post(WEBHOOK, json=unknown_body, params={"hmac": unknown_sig})

    _, other = await _paid_checkout(db_session)
    wrong_body, wrong_sig = _body(_transaction(reference=str(other.id), amount_cents=1))
    mismatched = await http.post(WEBHOOK, json=wrong_body, params={"hmac": wrong_sig})

    for response in (applied, duplicate, unmatched, mismatched):
        assert response.status_code == 200
        assert response.json() == {"status": "received"}


async def test_the_response_never_carries_payment_detail(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    invoice, payment = await _paid_checkout(db_session)
    body, signature = _body(_transaction(reference=str(payment.id)))

    response = await http.post(WEBHOOK, json=body, params={"hmac": signature})

    for leaked in (str(payment.id), str(invoice.id), "99.00", HMAC_SECRET, "sk_test"):
        assert leaked not in response.text


# ------------------------------------------------------------- idempotency


async def test_a_replayed_callback_settles_once(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The retry a provider sends when it did not get a 2xx in time."""
    invoice, payment = await _paid_checkout(db_session)
    body, signature = _body(_transaction(reference=str(payment.id)))

    for _ in range(4):
        assert (await http.post(WEBHOOK, json=body, params={"hmac": signature})).status_code == 200

    await db_session.flush()
    assert invoice.amount_paid == Decimal("99.00")
    events = list(
        (
            await db_session.execute(
                select(PaymentEvent).where(PaymentEvent.payment_id == payment.id),
            )
        ).scalars()
    )
    assert len(events) == 1


# ------------------------------------------------------------ secret hygiene


async def test_no_secret_reaches_a_log(
    http: AsyncClient,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the HMAC secret nor the API key, on the accepted or refused path.

    Written as a regression test because the failure mode is one `extra={...}`
    added while debugging a signature mismatch, which nothing else would see.
    """
    import logging

    invoice, payment = await _paid_checkout(db_session)
    body, signature = _body(_transaction(reference=str(payment.id)))

    with caplog.at_level(logging.DEBUG):
        await http.post(WEBHOOK, json=body, params={"hmac": signature})
        await http.post(WEBHOOK, json=body, params={"hmac": "0" * 128})

        # Only what *this application* wrote. `httpx` logs the request URL it
        # was given, which carries the `hmac` query parameter by design - that
        # is the test's own client talking, not the code under test, and
        # asserting on it would be asserting about a library.
        haystack: list[str] = []
        for record in caplog.records:
            if not record.name.startswith("app."):
                continue
            haystack.append(record.getMessage())
            haystack.extend(str(value) for value in record.__dict__.values())

    written = "\n".join(haystack)
    assert HMAC_SECRET not in written, "the signing secret reached a log"
    assert "sk_test_notreal" not in written, "the API key reached a log"
    # The digest itself is not secret - it travels in the URL - but the
    # *expected* one must never be written beside a rejected one, because that
    # would turn a refusal into an oracle telling a forger what to send next.
    assert signature not in written
