"""Resolving a charge nobody heard the answer to, without ever sending another.

The other half of ADR-088. `test_billing_crash_recovery.py` proves a dying
worker cannot cause a second debit; this proves the attempt it leaves behind
does not sit there for ever - and, more importantly, that every way of getting
it wrong is closed.

The distinctions that matter, each with its own test:

    provider says it succeeded    settle, exactly as a callback would
    provider says it failed       record the decline, spend the budget
    provider is still working     leave it, ask again
    provider has no record        wait, then hand the attempt back
    provider is unreachable       **learn nothing.** Never "no record."

The last pair is the one that would be expensive to confuse. A provider that is
down, read as one that never received the request, re-charges every card that
was in flight when the outage started.

The provider double is an `httpx.MockTransport` under the real `PaymobProvider`,
so the real inquiry requests are built, the real auth token is minted, and the
real response is parsed by the same `_event` a callback goes through.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider
from app.services.payment_reconciliation_service import PaymentReconciler

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
AMOUNT = Decimal("100.00")
GRACE = 300.0
LEASE = 900.0
ABANDON_AFTER = 86_400.0
API_KEY = "ZXlKaGJHY2lPaUpJVXpVeE1pSXNJblI1Y0NJNklrcFhWQ0o5LnRlc3Q"
TRANSACTION_ID = 700000999


def _transaction(reference: str, **overrides: Any) -> dict[str, Any]:
    """A Paymob transaction object, in the shape a callback carries.

    The inquiry API answers with the same object, which is the point: one
    translation, one settlement path, one meaning.
    """
    document = {
        "id": TRANSACTION_ID,
        "success": True,
        "pending": False,
        "error_occured": False,
        "is_refunded": False,
        "is_voided": False,
        "amount_cents": int(AMOUNT * 100),
        "currency": "EGP",
        "order": {"id": 4242, "merchant_order_id": reference},
    }
    document.update(overrides)
    return document


class Paymob:
    """Paymob's inquiry API, faked at the socket, with a scripted answer.

    Records every inquiry so a test can assert *how many times* the provider
    was asked, which is what proves a lease is doing its job.
    """

    def __init__(self) -> None:
        self.answer: httpx.Response | None = None
        self.error: Exception | None = None
        self.inquiries: list[str] = []
        self.tokens = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "auth/tokens" in url:
                self.tokens += 1
                return httpx.Response(201, json={"token": "a-bearer-token"})
            if "transaction_inquiry" in url:
                body = json.loads(request.content)
                self.inquiries.append(str(body.get("merchant_order_id")))
                if self.error is not None:
                    raise self.error
                return self.answer or httpx.Response(404, json={"detail": "Not found."})
            raise AssertionError(f"unexpected request: {url}")

        return httpx.MockTransport(handler)

    def provider(self, *, api_key: str | None = API_KEY) -> PaymobProvider:
        return PaymobProvider(
            secret_key="sk_test_notreal",
            public_key="pk_test_notreal",
            hmac_secret="a-test-hmac-secret",
            integration_ids=[4097558],
            moto_integration_id=9900001,
            api_key=api_key,
            transport=self.transport(),
        )


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, over an engine of this test's own.

    The reconciler claims, commits, asks and finalises in separate
    transactions, so a suite whose fixture never commits could not observe any
    of the properties that matter.
    """
    engine = create_async_engine(prepared_database, pool_size=6, max_overflow=4)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def attempt(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """A workspace whose renewal was charged and never answered for.

    Exactly what a killed worker leaves behind: an open invoice, one attempt
    against it, `requested`, `pending`, older than the grace period. Returns
    the tenant, the invoice and the payment.
    """
    async with committing() as session:
        tenant = Tenant(name="Recon", slug=f"recon-{uuid.uuid4().hex[:10]}")
        plan = Plan(
            code=f"recon-{uuid.uuid4().hex[:8]}",
            name="Recon",
            price=AMOUNT,
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.AGENTS.value: 5},
        )
        session.add_all([tenant, plan])
        await session.flush()

        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status=SubscriptionStatus.PAST_DUE,
            current_period_start=NOW - timedelta(days=35),
            current_period_end=NOW + timedelta(days=25),
        )
        session.add(subscription)
        await session.flush()

        invoice = Invoice(
            tenant_id=tenant.id,
            subscription_id=subscription.id,
            status=InvoiceStatus.OPEN,
            plan_code=plan.code,
            amount_due=AMOUNT,
            amount_paid=Decimal("0.00"),
            currency="EGP",
            period_start=NOW - timedelta(days=35),
            period_end=NOW - timedelta(days=5),
            issued_at=NOW - timedelta(days=5),
            lines=[],
            collection_attempts=1,
            next_collection_at=NOW + timedelta(days=1),
        )
        session.add(invoice)
        await session.flush()

        payment = Payment(
            tenant_id=tenant.id,
            invoice_id=invoice.id,
            status=PaymentStatus.PENDING,
            amount=AMOUNT,
            currency="EGP",
            provider="paymob",
            is_automatic=True,
            collection_state=CollectionState.REQUESTED,
            idempotency_key=f"auto:{invoice.id}:1",
            refunded_amount=Decimal("0.00"),
        )
        session.add(payment)
        await session.flush()

        # Older than the grace period, so it is nobody's live work. Written
        # directly because `created_at` is a server default.
        payment.created_at = NOW - timedelta(seconds=GRACE + 60)
        await session.commit()
        identifiers = (tenant.id, invoice.id, payment.id)

    try:
        yield identifiers
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == identifiers[0]))
            await session.execute(
                delete(Plan).where(Plan.code.like("recon-%")).where(Plan.name == "Recon")
            )
            await session.commit()


async def _run(
    committing: async_sessionmaker[AsyncSession],
    paymob: Paymob,
    *,
    now: datetime = NOW,
    api_key: str | None = API_KEY,
) -> Any:
    async with committing() as session:
        reconciler = PaymentReconciler(session=session, provider=paymob.provider(api_key=api_key))
        return await reconciler.run(
            now=now,
            grace_seconds=GRACE,
            lease_seconds=LEASE,
            abandon_after_seconds=ABANDON_AFTER,
            limit=10,
        )


async def _payment(
    committing: async_sessionmaker[AsyncSession],
    payment_id: uuid.UUID,
) -> Payment:
    async with committing() as session:
        row = await session.get(Payment, payment_id, populate_existing=True)
        assert row is not None
        return row


async def _invoice(
    committing: async_sessionmaker[AsyncSession],
    invoice_id: uuid.UUID,
) -> Invoice:
    async with committing() as session:
        row = await session.get(Invoice, invoice_id, populate_existing=True)
        assert row is not None
        return row


# ----------------------------------------------------------- the five answers


async def test_a_confirmed_charge_settles_the_invoice(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """GATE: the money did move, and the ledger catches up.

    This is the recovery a killed worker needs. The customer was charged, the
    callback never arrived or arrived while nothing was listening, and the
    invoice would otherwise stay open until dunning suspended somebody who had
    already paid.
    """
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id)))

    outcome = await _run(committing, paymob)

    assert outcome.settled == 1
    assert paymob.inquiries == [str(payment_id)], "asked about our own reference and nothing else"

    payment = await _payment(committing, payment_id)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.collection_state is CollectionState.SETTLED
    assert payment.provider_reference == str(TRANSACTION_ID)

    invoice = await _invoice(committing, invoice_id)
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == AMOUNT


async def test_a_confirmed_refusal_is_recorded_and_the_budget_stays_spent(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The card was tried and declined. The attempt counts."""
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(
        200,
        json=_transaction(
            str(payment_id),
            success=False,
            error_occured=True,
            data={"message": "Insufficient funds"},
        ),
    )

    outcome = await _run(committing, paymob)

    assert outcome.failed == 1
    payment = await _payment(committing, payment_id)
    assert payment.status is PaymentStatus.FAILED
    assert payment.collection_state is CollectionState.SETTLED

    invoice = await _invoice(committing, invoice_id)
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.collection_attempts == 1, "a decline spends the attempt it used"


async def test_a_still_pending_charge_is_left_alone(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The provider has not finished. Nothing to do but ask again."""
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id), pending=True))

    outcome = await _run(committing, paymob)

    assert outcome.still_pending == 1
    payment = await _payment(committing, payment_id)
    assert payment.status is PaymentStatus.PENDING
    assert payment.collection_state is CollectionState.REQUESTED

    invoice = await _invoice(committing, invoice_id)
    assert invoice.status is InvoiceStatus.OPEN


async def test_an_unreachable_provider_is_not_read_as_a_missing_payment(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """GATE: the distinction the whole outage story rests on.

    A provider that is down and a provider that never received the request
    give a reconciler nothing in common except silence. Reading the first as
    the second hands the attempt back, and the next sweep charges every card
    that was in flight when the outage began.
    """
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.error = httpx.ConnectError("paymob is down")

    outcome = await _run(committing, paymob)

    assert outcome.unreachable == 1
    assert outcome.abandoned == 0, "an outage must not close an attempt"
    assert outcome.not_found == 0, "and must not be reported as an absent one"

    payment = await _payment(committing, payment_id)
    assert payment.status is PaymentStatus.PENDING
    assert payment.collection_state is CollectionState.REQUESTED, "still nobody's answer"

    invoice = await _invoice(committing, invoice_id)
    assert invoice.collection_attempts == 1, "the attempt was not given back"


async def test_a_reference_the_provider_does_not_know_waits_before_it_is_believed(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """ "No such order" is evidence, not a verdict.

    A provider that has not finished making an event visible answers exactly
    as one that never received the request. Only time separates them, so a
    fresh attempt is left alone however confidently Paymob says it has never
    heard of it.
    """
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(404, json={"detail": "Not found."})

    outcome = await _run(committing, paymob)

    assert outcome.not_found == 1
    assert outcome.abandoned == 0, "too soon to believe"

    payment = await _payment(committing, payment_id)
    assert payment.collection_state is CollectionState.REQUESTED
    invoice = await _invoice(committing, invoice_id)
    assert invoice.collection_attempts == 1


async def test_a_reference_still_unknown_a_day_later_hands_the_attempt_back(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Once the answer has stopped changing, the attempt closes and returns.

    The invoice becomes collectible again - by a *new* attempt with a new
    number and a new reference. Nothing re-sends the old one.
    """
    _, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(404, json={"detail": "Not found."})

    outcome = await _run(committing, paymob, now=NOW + timedelta(days=2))

    assert outcome.abandoned == 1
    payment = await _payment(committing, payment_id)
    assert payment.status is PaymentStatus.FAILED
    assert payment.collection_state is CollectionState.ABANDONED
    assert payment.is_unresolved_collection is False

    invoice = await _invoice(committing, invoice_id)
    assert invoice.collection_attempts == 0, "a charge that never left costs no attempt"
    assert invoice.next_collection_at is None, "and the invoice is due again"


# --------------------------------------------------------------- the races


async def test_a_callback_can_settle_an_attempt_after_the_worker_dies(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """GATE: the second half of WSL-01, closed.

    The old failure: the callback quoted a payment id whose row had been rolled
    back, so `_tenant_for` resolved nothing, the endpoint logged
    `callback_unknown_payment` and answered 200 - and the customer who had paid
    was eventually marked past due and suspended.

    The reference now names a row that was committed before Paymob was told
    about it, so the callback lands.
    """
    tenant_id, invoice_id, payment_id = attempt

    async with committing() as session:
        found = await session.execute(select(Payment.tenant_id).where(Payment.id == payment_id))
        resolved = found.scalar_one_or_none()
    assert resolved == tenant_id, "the callback's own lookup has something to find"

    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id)))
    outcome = await _run(committing, paymob)

    assert outcome.settled == 1
    invoice = await _invoice(committing, invoice_id)
    assert invoice.status is InvoiceStatus.PAID


async def test_the_callback_and_the_reconciler_settle_exactly_once(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """GATE: two things learn the same fact at once, and one settlement happens.

    Both routes build the same event id from the same transaction, so
    `UNIQUE(provider, provider_event_id)` on `payment_events` decides which of
    them does the work. That is a database constraint rather than a status
    check, which is what makes it hold under a genuine interleaving rather than
    only under the one the test happened to produce.

    Driven here by running reconciliation twice against the same answer: the
    second pass is indistinguishable from a callback arriving a moment late.
    """
    tenant_id, invoice_id, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id)))

    first = await _run(committing, paymob)
    # The lease is cleared so the second pass really re-examines the row rather
    # than skipping it - the race being modelled is two settlers, not one.
    async with committing() as session:
        row = await session.get(Payment, payment_id, populate_existing=True)
        assert row is not None
        row.collection_state = CollectionState.REQUESTED
        row.reconciled_at = None
        await session.commit()
    second = await _run(committing, paymob)

    assert first.settled == 1
    assert second.settled + second.failed <= 1

    async with committing() as session:
        events = await session.execute(
            select(PaymentEvent).where(PaymentEvent.payment_id == payment_id)
        )
        recorded = list(events.scalars())
        applied = [row for row in recorded if row.outcome == "applied"]

    assert len(applied) == 1, "one settlement, however many things noticed"

    invoice = await _invoice(committing, invoice_id)
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == AMOUNT, "the invoice was not paid twice"

    async with committing() as session:
        subscriptions = await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
        subscription = subscriptions.scalars().one()
    assert subscription.status is SubscriptionStatus.ACTIVE, "restored once"


async def test_a_leased_attempt_is_not_asked_about_twice(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The lease survives the transaction that took it.

    Two reconcilers running in sequence inside one lease window ask the
    provider once between them, because the claim is committed before the
    lookup rather than being a row lock that ends with the transaction.
    """
    _, _, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id), pending=True))

    await _run(committing, paymob)
    await _run(committing, paymob, now=NOW + timedelta(seconds=60))

    assert paymob.inquiries == [str(payment_id)], "the second pass re-asked a leased attempt"


async def test_an_expired_lease_is_reclaimed_without_a_reaper(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """A reconciler that died mid-lookup strands nothing.

    Nothing exists to notice that it did. The lease simply becomes old enough
    to ignore, which is why there is no second recovery mechanism guarding the
    first one.
    """
    _, _, payment_id = attempt
    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id), pending=True))

    await _run(committing, paymob)
    await _run(committing, paymob, now=NOW + timedelta(seconds=LEASE + 60))

    assert paymob.inquiries == [str(payment_id), str(payment_id)]


# ------------------------------------------------------------ configuration


async def test_a_deployment_that_cannot_ask_changes_nothing(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """No inquiry credential is a supported state, and a safe one.

    The attempt stays unresolved and the invoice stays uncollectible, which is
    slower than a deployment that can ask and is not a duplicate charge.
    """
    _, invoice_id, payment_id = attempt
    paymob = Paymob()

    outcome = await _run(committing, paymob, api_key=None)

    assert outcome.examined == 0
    assert paymob.inquiries == [], "nothing was asked"
    assert paymob.tokens == 0

    payment = await _payment(committing, payment_id)
    assert payment.collection_state is CollectionState.REQUESTED
    invoice = await _invoice(committing, invoice_id)
    assert invoice.collection_attempts == 1


async def test_an_attempt_inside_the_grace_period_is_left_to_its_worker(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """A charge made a moment ago belongs to the job that made it.

    Asking about it there races a settlement that is about to happen anyway,
    and the losing side of that race writes over the winner.
    """
    _, _, payment_id = attempt
    async with committing() as session:
        row = await session.get(Payment, payment_id, populate_existing=True)
        assert row is not None
        row.created_at = NOW - timedelta(seconds=10)
        await session.commit()

    paymob = Paymob()
    paymob.answer = httpx.Response(200, json=_transaction(str(payment_id)))

    outcome = await _run(committing, paymob)

    assert outcome.examined == 0
    assert paymob.inquiries == []


async def test_a_settled_attempt_is_never_examined_again(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Terminal is terminal. Reconciliation reads unresolved attempts only."""
    _, _, payment_id = attempt
    async with committing() as session:
        row = await session.get(Payment, payment_id, populate_existing=True)
        assert row is not None
        row.status = PaymentStatus.SUCCEEDED
        row.collection_state = CollectionState.SETTLED
        await session.commit()

    paymob = Paymob()
    outcome = await _run(committing, paymob)

    assert outcome.examined == 0
    assert paymob.inquiries == []


async def test_a_hosted_checkout_is_never_reconciled(
    committing: async_sessionmaker[AsyncSession],
    attempt: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """An abandoned payment page is not an unanswered charge.

    A customer who opened a checkout and closed the tab leaves a `pending`
    payment for ever, and it is nothing to do with this. The collection state
    is what separates them, which is why it is NULL for everything a person
    was present for.
    """
    _, _, payment_id = attempt
    async with committing() as session:
        row = await session.get(Payment, payment_id, populate_existing=True)
        assert row is not None
        row.is_automatic = False
        row.collection_state = None
        await session.commit()

    paymob = Paymob()
    outcome = await _run(committing, paymob)

    assert outcome.examined == 0
    assert paymob.inquiries == []
