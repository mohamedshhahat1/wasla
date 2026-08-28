"""Checkout and callbacks against a real database.

The threat is not subtle: **a callback endpoint that can be persuaded to settle
an invoice is a way to get the product for free.** It is unauthenticated by
necessity, anybody can send it bytes, and what it decides is whether money
arrived. So most of what is below is about the four refusals that stand between
a verified callback and a paid invoice - new, ours, this workspace's, and for
the amount we asked - plus the ones about not letting a customer choose their
own price on the way in.

The provider is faked at the HTTP boundary rather than mocked out, so the real
`PaymobProvider` runs: the real intention body is built, the real response is
parsed, and the real HMAC verification happens against payloads signed the way
Paymob signs them. A test double in place of the provider would prove that
`CheckoutService` calls something, which is not the interesting part.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    SubscriptionStatus,
)
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.services.checkout_service import (
    APPLIED,
    DUPLICATE,
    MISMATCHED,
    NO_CHANGE,
    REFUSED,
    UNMATCHED,
    CheckoutService,
)

pytestmark = pytest.mark.integration

HMAC_SECRET = "a-test-hmac-secret"
CLIENT_SECRET = "egy_csk_test_0123456789abcdef"
INTENTION_ID = "pi_test_0123456789abcdef"


def _intention_transport(captured: list[dict] | None = None) -> httpx.MockTransport:
    """A stand-in for Paymob's intention endpoint.

    Records the body we sent, which is how the amount tests assert on what was
    actually asked for rather than on what the service intended to ask for.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "id": INTENTION_ID,
                "client_secret": CLIENT_SECRET,
                "intention_order_id": 265715202,
            },
        )

    return httpx.MockTransport(handler)


def _provider(transport: httpx.MockTransport | None = None) -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
        transport=transport if transport is not None else _intention_transport(),
    )


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _user(session, email: str = "owner@acme-example.com") -> User:
    user = User(email=email, full_name="Owner Person", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    return user


async def _plan(
    session,
    *,
    code: str = "pro",
    price: str = "99.00",
    currency: str = "EGP",
    is_public: bool = True,
) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal(price),
        currency=currency,
        interval=BillingInterval.MONTHLY,
        is_public=is_public,
        limits={LimitKey.AGENTS.value: 5},
    )
    session.add(plan)
    await session.flush()
    return plan


def _service(session, tenant, *, transport=None) -> CheckoutService:
    return CheckoutService(
        session,
        tenant_id=tenant.id,
        provider=_provider(transport),
    )


def _transaction(*, reference: str | None, amount_cents: int = 9900, **overrides) -> dict:
    """A transaction shaped like the documented callback."""
    transaction = {
        "id": 192036465,
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


def _callback(transaction: dict) -> tuple[bytes, str]:
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    return body, hmac_signature(transaction, secret=HMAC_SECRET)


async def _verified(transaction: dict):
    body, signature = _callback(transaction)
    return _provider().verify_callback(payload=body, signature=signature)


# ----------------------------------------------------------------- checkout


async def test_a_checkout_prices_the_plan_from_the_database(db_session):
    """The amount sent to the provider is ours, in cents, and nobody else's."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session, price="99.00")
    captured: list[dict] = []

    started = await _service(db_session, tenant, transport=_intention_transport(captured)).start(
        plan_code="pro", actor=user
    )

    assert started.amount == Decimal("99.00")
    assert started.currency == "EGP"
    assert captured[0]["amount"] == 9900, "Paymob takes integer cents"
    assert captured[0]["currency"] == "EGP"


async def test_the_redirect_url_is_the_documented_checkout_url(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)

    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    assert started.redirect_url == (
        f"https://eg.checkout.paymob.com/?publicKey=pk_test_notreal&clientSecret={CLIENT_SECRET}"
    )


async def test_the_reference_sent_is_our_own_payment_id(db_session):
    """The whole callback-to-row mapping, and it must be ours.

    A reference the provider or the browser chose would be a reference an
    attacker could aim at another workspace's payment.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    captured: list[dict] = []

    started = await _service(db_session, tenant, transport=_intention_transport(captured)).start(
        plan_code="pro", actor=user
    )

    assert captured[0]["special_reference"] == str(started.payment_id)


async def test_a_checkout_leaves_an_invoice_and_a_pending_payment(db_session):
    """Written before the provider is called, so a callback can never arrive
    for a payment this system has not heard of."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)

    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    payment = (
        await db_session.execute(select(Payment).where(Payment.id == started.payment_id))
    ).scalar_one()

    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_due == Decimal("99.00")
    assert payment.status is PaymentStatus.PENDING
    assert payment.provider == "paymob"
    assert payment.provider_reference is None, "no transaction exists until somebody pays"
    assert payment.provider_intent_reference == INTENTION_ID


async def test_a_private_plan_cannot_be_paid_for_either(db_session):
    """Checkout is another door onto plan selection and gets the same lock."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session, code="enterprise", is_public=False)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).start(plan_code="enterprise", actor=user)


async def test_a_free_plan_is_not_sent_to_a_payment_page(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session, code="free", price="0.00")

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).start(plan_code="free", actor=user)


async def test_a_second_attempt_reuses_the_invoice_rather_than_billing_twice(db_session):
    """`UNIQUE(tenant_id, period_start)` decides this either way.

    Somebody who abandons a checkout and starts another gets a second payment
    against one invoice - attempts are rows, which is what the payments table
    is for.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    service = _service(db_session, tenant)

    first = await service.start(plan_code="pro", actor=user)
    second = await service.start(plan_code="pro", actor=user)

    assert first.invoice_id == second.invoice_id
    assert first.payment_id != second.payment_id


async def test_a_paid_period_is_not_charged_again(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    service = _service(db_session, tenant)
    started = await service.start(plan_code="pro", actor=user)

    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    invoice.status = InvoiceStatus.PAID
    await db_session.flush()

    with pytest.raises(ConflictError):
        await service.start(plan_code="pro", actor=user)


async def test_a_provider_failure_does_not_look_like_a_payment(db_session):
    """A 5xx from the processor raises. It must never become a settled invoice."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    broken = httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"}))

    from app.integrations.billing.base import ProviderError

    with pytest.raises(ProviderError):
        await _service(db_session, tenant, transport=broken).start(plan_code="pro", actor=user)


async def test_a_response_without_a_client_secret_is_a_provider_failure(db_session):
    """A 2xx missing the one field the flow needs is not something to parse
    around: a checkout URL built from a missing secret is a broken page."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    empty = httpx.MockTransport(lambda request: httpx.Response(201, json={"id": "pi_x"}))

    from app.integrations.billing.base import ProviderError

    with pytest.raises(ProviderError):
        await _service(db_session, tenant, transport=empty).start(plan_code="pro", actor=user)


# ---------------------------------------------------------------- callbacks


async def _started(db_session, tenant, user, **plan_kwargs):
    await _plan(db_session, **plan_kwargs)
    return await _service(db_session, tenant).start(
        plan_code=plan_kwargs.get("code", "pro"), actor=user
    )


async def test_a_verified_callback_settles_the_invoice(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(_transaction(reference=str(started.payment_id)))
    outcome = await _service(db_session, tenant).apply(event)

    assert outcome == APPLIED
    payment = (
        await db_session.execute(select(Payment).where(Payment.id == started.payment_id))
    ).scalar_one()
    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.provider_reference == "192036465"
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == Decimal("99.00")


async def test_the_same_callback_twice_settles_once(db_session):
    """The constraint decides it, not a preceding read.

    A provider retries anything it did not get a 2xx for, and processing a
    retry twice records money that arrived once as money that arrived twice.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)
    event = await _verified(_transaction(reference=str(started.payment_id)))
    service = _service(db_session, tenant)

    assert await service.apply(event) == APPLIED
    assert await service.apply(event) == DUPLICATE

    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert invoice.amount_paid == Decimal("99.00"), "paid once, not twice"

    events = list(
        (await db_session.execute(select(PaymentEvent))).scalars(),
    )
    assert len(events) == 1


async def test_a_callback_naming_a_payment_we_never_issued_is_refused(db_session):
    tenant = await _tenant(db_session)
    event = await _verified(_transaction(reference=str(uuid.uuid4())))

    assert await _service(db_session, tenant).apply(event) == UNMATCHED


async def test_a_callback_with_no_reference_is_refused(db_session):
    tenant = await _tenant(db_session)
    event = await _verified(_transaction(reference=None))

    assert await _service(db_session, tenant).apply(event) == UNMATCHED


async def test_a_callback_reporting_a_different_amount_is_refused(db_session):
    """The forgery this check exists for: a real order, a smaller payment.

    A provider that says it collected something other than what we asked for
    has done something we do not understand, and settling anyway would paper
    over it.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(
        _transaction(reference=str(started.payment_id), amount_cents=1),
    )
    outcome = await _service(db_session, tenant).apply(event)

    assert outcome == MISMATCHED
    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")


async def test_a_callback_in_a_different_currency_is_refused(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(
        _transaction(reference=str(started.payment_id), currency="USD"),
    )

    assert await _service(db_session, tenant).apply(event) == MISMATCHED


async def test_a_failed_payment_does_not_settle_anything(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(
        _transaction(
            reference=str(started.payment_id),
            success=False,
            error_occured=True,
            data={"message": "Declined"},
        ),
    )
    assert await _service(db_session, tenant).apply(event) == APPLIED

    payment = (
        await db_session.execute(select(Payment).where(Payment.id == started.payment_id))
    ).scalar_one()
    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == "Declined"
    assert invoice.status is InvoiceStatus.OPEN


async def test_a_payment_still_in_flight_does_not_settle_anything(db_session):
    """`success` true and `pending` true is money that has not moved."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(
        _transaction(reference=str(started.payment_id), success=True, pending=True),
    )
    await _service(db_session, tenant).apply(event)

    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert invoice.status is InvoiceStatus.OPEN


# ------------------------------------------------------- subscription effect


async def test_a_payment_revives_a_past_due_subscription(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    plan = await _plan(db_session)
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    subscription = SubscriptionRepository(db_session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.PAST_DUE,
        current_period_start=now - timedelta(days=5),
        current_period_end=now + timedelta(days=25),
    )
    await db_session.flush()

    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)
    event = await _verified(_transaction(reference=str(started.payment_id)))
    await _service(db_session, tenant).apply(event)

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.ACTIVE


async def test_a_payment_does_not_revive_a_cancelled_subscription(db_session):
    """Paying an invoice is not a request to resubscribe.

    Reviving here would undo a decision somebody made deliberately, on the
    strength of a payment for a period they had already been billed for.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    plan = await _plan(db_session)
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    subscription = SubscriptionRepository(db_session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.CANCELLED,
        current_period_start=now - timedelta(days=5),
        current_period_end=now + timedelta(days=25),
    )
    await db_session.flush()

    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)
    event = await _verified(_transaction(reference=str(started.payment_id)))
    await _service(db_session, tenant).apply(event)

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.CANCELLED


# ----------------------------------------------------------- tenant isolation


async def test_a_callback_cannot_settle_another_workspaces_payment(db_session):
    """The isolation boundary, exercised directly.

    Even holding a real payment reference from workspace A, a service scoped to
    workspace B resolves it to nothing - the repository's tenant filter is what
    makes that true rather than a check somebody remembered to write.
    """
    alpha = await _tenant(db_session, slug="alpha")
    beta = await _tenant(db_session, slug="beta")
    user = await _user(db_session)
    await _plan(db_session)

    started = await _service(db_session, alpha).start(plan_code="pro", actor=user)
    event = await _verified(_transaction(reference=str(started.payment_id)))

    outcome = await _service(db_session, beta).apply(event)

    assert outcome == UNMATCHED
    invoice = (
        await db_session.execute(select(Invoice).where(Invoice.id == started.invoice_id))
    ).scalar_one()
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")


async def test_one_workspaces_checkout_never_touches_anothers_invoice(db_session):
    alpha = await _tenant(db_session, slug="alpha")
    beta = await _tenant(db_session, slug="beta")
    user = await _user(db_session)
    await _plan(db_session)

    first = await _service(db_session, alpha).start(plan_code="pro", actor=user)
    second = await _service(db_session, beta).start(plan_code="pro", actor=user)

    assert first.invoice_id != second.invoice_id
    invoices = list((await db_session.execute(select(Invoice))).scalars())
    assert {invoice.tenant_id for invoice in invoices} == {alpha.id, beta.id}


# -------------------------------------------------------------- card data


async def test_no_card_number_is_ever_stored(db_session):
    """Paymob sends the last four digits and nothing else, and even those are
    not persisted. A PAN column is a compliance problem this product does not
    want to have."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    started = await _started(db_session, tenant, user)

    event = await _verified(_transaction(reference=str(started.payment_id)))
    await _service(db_session, tenant).apply(event)

    payment = (
        await db_session.execute(select(Payment).where(Payment.id == started.payment_id))
    ).scalar_one()
    stored = " ".join(
        str(value)
        for value in (
            payment.provider_reference,
            payment.provider_intent_reference,
            payment.failure_reason,
        )
    )
    assert "2346" not in stored
    assert "MasterCard" not in stored


# ------------------------------------------------------------- the ledger


async def _events(session) -> list[PaymentEvent]:
    rows = await session.execute(select(PaymentEvent).order_by(PaymentEvent.received_at))
    return list(rows.scalars().all())


async def test_the_ledger_records_what_actually_happened_not_what_was_hoped(db_session):
    """Every callback used to be filed as `applied`, whatever was decided.

    An event naming an unknown payment, or reporting an amount that disagreed
    with the invoice, was refused and then recorded as a success - so the one
    table an operator would read to find out why a customer's payment never
    landed said that it had.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(uuid.uuid4())))
    )
    await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(started.payment_id), amount_cents=1, id=111111))
    )
    await db_session.flush()

    outcomes = {event.outcome for event in await _events(db_session)}
    assert outcomes == {UNMATCHED, MISMATCHED}


async def test_an_applied_event_says_what_it_did(db_session):
    """`detail` is written by this application and never by the provider.

    The callback body carries a masked card number, the customer's billing
    details and a redirect URL containing a bearer token. None of it is stored,
    which is why this field is a sentence of ours rather than an excerpt.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(started.payment_id)))
    )
    await db_session.flush()

    event = (await _events(db_session))[0]
    assert event.outcome == APPLIED
    assert event.event_type == "transaction.succeeded"
    assert event.detail == "Invoice paid."
    assert event.provider_transaction_id == "192036465"
    assert event.received_at is not None
    assert event.processed_at is not None


async def test_the_event_id_is_not_the_bare_transaction_id(db_session):
    """They are kept in separate columns because they answer separate questions.

    `provider_event_id` decides idempotency and pairs the transaction with the
    state reported; `provider_transaction_id` is the number the provider's
    dashboard and a support conversation both use.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(started.payment_id)))
    )
    await db_session.flush()

    event = (await _events(db_session))[0]
    assert event.provider_event_id == "192036465:succeeded"
    assert event.provider_transaction_id == "192036465"


async def test_a_transaction_reporting_pending_then_success_settles_once(db_session):
    """The 3-D Secure sequence, which is two callbacks about one transaction.

    Keying idempotency on the transaction id alone would file the success as a
    duplicate of the pending notification and settle nothing at all - a payment
    the customer made and the system never recorded.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    in_flight = await _service(db_session, tenant).apply(
        await _verified(
            _transaction(reference=str(started.payment_id), pending=True, success=False)
        )
    )
    settled = await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(started.payment_id)))
    )
    await db_session.flush()

    # The pending notification says nothing new - the attempt was already
    # pending - and is recorded as such. What matters is the next line: the
    # success is *not* swallowed as a duplicate of it.
    assert in_flight == NO_CHANGE
    assert settled == APPLIED
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == Decimal("99.00")


async def test_a_second_payment_against_a_settled_invoice_is_refused(db_session):
    """A customer who paid twice is a refund to issue, not a bigger balance.

    Without the invoice transition table this added `amount_paid` a second
    time, leaving an invoice recording 198.00 collected against 99.00 due.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)
    first = await _service(db_session, tenant).start(plan_code="pro", actor=user)
    second = await _service(db_session, tenant).start(plan_code="pro", actor=user)
    assert first.invoice_id == second.invoice_id

    await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(first.payment_id)))
    )
    outcome = await _service(db_session, tenant).apply(
        await _verified(_transaction(reference=str(second.payment_id), id=222222))
    )
    await db_session.flush()

    assert outcome == REFUSED
    invoice = await db_session.get(Invoice, first.invoice_id)
    assert invoice is not None
    assert invoice.amount_paid == Decimal("99.00")


# ------------------------------------------------------- paying a renewal


async def test_a_checkout_can_collect_an_invoice_the_sweep_issued(db_session):
    """What makes the billing cycle collectible rather than merely recorded.

    A renewal invoice is produced by the worker at every period end, and until
    now the only way to open a payment page was to pick a plan - which issues
    an invoice for the *current* period rather than paying the one that is
    outstanding.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    plan = await _plan(db_session)
    invoice = InvoiceRepository(db_session, tenant_id=tenant.id).create(
        subscription_id=None,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
        lines=[],
    )
    await db_session.flush()

    started = await _service(db_session, tenant).start(invoice_id=invoice.id, actor=user)

    assert started.invoice_id == invoice.id
    assert started.amount == Decimal("99.00")


async def test_a_checkout_collects_what_is_left_rather_than_the_whole_bill(db_session):
    """Part-paid invoices are a real state, and charging the full amount again
    would take money the customer does not owe."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    plan = await _plan(db_session)
    invoice = InvoiceRepository(db_session, tenant_id=tenant.id).create(
        subscription_id=None,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
        lines=[],
    )
    await db_session.flush()
    invoice.amount_paid = Decimal("40.00")
    await db_session.flush()

    started = await _service(db_session, tenant).start(invoice_id=invoice.id, actor=user)

    assert started.amount == Decimal("59.00")


async def test_a_paid_invoice_cannot_be_collected_again(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice = InvoiceRepository(db_session, tenant_id=tenant.id).create(
        subscription_id=None,
        status=InvoiceStatus.PAID,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
        lines=[],
    )
    await db_session.flush()

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).start(invoice_id=invoice.id)


async def test_a_withdrawn_invoice_cannot_be_collected(db_session):
    """A bill the customer was told to ignore must not become a payment page."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice = InvoiceRepository(db_session, tenant_id=tenant.id).create(
        subscription_id=None,
        status=InvoiceStatus.VOID,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
        lines=[],
    )
    await db_session.flush()

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).start(invoice_id=invoice.id)


async def test_another_workspaces_invoice_is_not_found(db_session):
    """Paying somebody else's bill is not generosity, it is a way in.

    A settled invoice moves that workspace's subscription out of `past_due`.
    """
    acme = await _tenant(db_session, "acme")
    globex = await _tenant(db_session, "globex")
    plan = await _plan(db_session)
    invoice = InvoiceRepository(db_session, tenant_id=globex.id).create(
        subscription_id=None,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 1, tzinfo=UTC),
        lines=[],
    )
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await _service(db_session, acme).start(invoice_id=invoice.id)


async def test_naming_neither_a_plan_nor_an_invoice_is_refused(db_session):
    tenant = await _tenant(db_session)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).start()


# ---------------------------------------------------- credentials in logs


async def test_no_log_line_carries_the_client_secret(db_session, caplog):
    """A canary, because this is the leak nothing else would catch.

    The client secret is a bearer value for one payment page. It has to reach
    the customer's browser, so it is inside `redirect_url` - and the moment
    anything logs that URL "for debugging", the secret is in whatever collects
    logs, for as long as that keeps them.

    Scoped to `app.` loggers on purpose: httpx logs the request URL it was
    given, and that URL is Paymob's API endpoint rather than the checkout page.
    A canary broad enough to catch a third-party library's own URL logging
    catches something we are not deciding.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)

    with caplog.at_level(logging.DEBUG):
        started = await _service(db_session, tenant).start(plan_code="pro", actor=user)

    assert CLIENT_SECRET in started.redirect_url, "the canary needs something to find"
    ours = [record for record in caplog.records if record.name.startswith("app.")]
    assert ours, "the walk found no application log records"
    for record in ours:
        rendered = record.getMessage() + repr(record.__dict__)
        assert CLIENT_SECRET not in rendered
        assert "eg.checkout.paymob.com" not in rendered


async def test_no_log_line_carries_the_secret_key(db_session, caplog):
    """The other half. This one authenticates *us* to Paymob.

    Anybody who read it out of a log could create charges against the merchant
    account, so it belongs in exactly one `Authorization` header and nowhere
    else.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    await _plan(db_session)

    with caplog.at_level(logging.DEBUG):
        await _service(db_session, tenant).start(plan_code="pro", actor=user)

    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert "sk_test_notreal" not in rendered
        assert HMAC_SECRET not in rendered


def test_the_client_secret_is_not_a_field_on_anything_persisted() -> None:
    """Structural, so it survives a refactor that nobody thinks about.

    `StartedCheckout` is what the service hands back and `Payment` is what is
    stored. Neither may grow a place to put a client secret: a column would
    keep it for ever, and a response field would invite a client to log it.
    """
    from dataclasses import fields

    from app.services.checkout_service import StartedCheckout

    names = {field.name for field in fields(StartedCheckout)}
    assert not {name for name in names if "secret" in name}

    columns = {column.name for column in Payment.__table__.columns}
    assert not {name for name in columns if "secret" in name}
