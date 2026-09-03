"""Two things happening at once, against a real database, really committing.

Every other integration test in this suite runs inside one transaction that is
rolled back at the end, which is fast and correct for almost everything - and
useless here. A unique constraint is not exercised by two coroutines sharing a
session: they share the same transaction, so the second write sees the first
one's uncommitted row and there is no race to lose. A test written that way
passes whether or not the constraint exists.

So this file opens its own engine and its own connections, commits for real,
and cleans up after itself. It is slower than the rest of the suite and it is
the only way the following are actually demonstrated:

- **A retried callback settles an invoice once.** Provider retries arrive
  concurrently by nature - that is what a retry storm is - and the whole
  idempotency design is a unique constraint decided by the database rather
  than a check decided by Python.
- **Two checkouts started at once bill one period once.** The invoice
  constraint is what stops a double bill, and the loser has to re-read rather
  than answer 500 to a customer who did nothing wrong.
- **One idempotency key opens one payment page.**

The cleanup is by tenant, which cascades to invoices, payments and the events
that reference them. Plans are global and are deleted by hand.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError
from app.db.models.billing import BillingInterval, LimitKey, Plan
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.services.checkout_service import (
    APPLIED,
    DUPLICATE,
    CheckoutService,
    StartedCheckout,
)

pytestmark = pytest.mark.integration

HMAC_SECRET = "a-test-hmac-secret"


def _provider() -> PaymobProvider:
    """A provider whose intention endpoint always answers.

    Each call returns a distinct client secret so a test can tell two intentions
    apart, which is how "did we create two payment pages" is actually asked.
    """
    counter = iter(range(1, 1000))

    def handler(request: httpx.Request) -> httpx.Response:
        index = next(counter)
        return httpx.Response(
            201,
            json={"id": f"pi_test_{index}", "client_secret": f"csk_test_{index}"},
        )

    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
        transport=httpx.MockTransport(handler),
    )


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, over an engine of this test's own.

    `NullPool` so every session takes a fresh connection - the point of the
    file is that the two callers are genuinely separate, and a pooled
    connection handed back and forth would quietly make them one.
    """
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A committed tenant and plan, removed afterwards whatever happens.

    Returns their ids rather than the objects: every session below is separate,
    and an instance loaded in one is not usable in another.
    """
    slug = f"conc-{uuid.uuid4().hex[:10]}"
    async with committing() as session:
        tenant = Tenant(name="Concurrent", slug=slug)
        plan = Plan(
            code=f"pro-{slug}",
            name="Pro",
            price=Decimal("99.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.AGENTS.value: 5},
        )
        session.add_all([tenant, plan])
        await session.commit()
        ids = (tenant.id, plan.id)

    try:
        yield ids
    finally:
        async with committing() as session:
            # Tenant cascades to invoices and payments; payment_events cascade
            # from payments. The plan is platform-wide and goes by hand.
            await session.execute(delete(Tenant).where(Tenant.id == ids[0]))
            await session.execute(delete(Plan).where(Plan.id == ids[1]))
            await session.commit()


async def _committed_invoice(
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    amount: str = "99.00",
) -> tuple[uuid.UUID, uuid.UUID]:
    """An open invoice with a pending attempt against it, committed."""
    async with committing() as session:
        moment = datetime(2026, 8, 1, tzinfo=UTC)
        invoice = Invoice(
            tenant_id=tenant_id,
            status=InvoiceStatus.OPEN,
            plan_code="pro",
            amount_due=Decimal(amount),
            amount_paid=Decimal("0.00"),
            currency="EGP",
            period_start=moment,
            period_end=moment,
            lines=[],
        )
        session.add(invoice)
        await session.flush()
        payment = Payment(
            tenant_id=tenant_id,
            invoice_id=invoice.id,
            status=PaymentStatus.PENDING,
            amount=Decimal(amount),
            currency="EGP",
            provider="paymob",
            refunded_amount=Decimal("0.00"),
        )
        session.add(payment)
        await session.commit()
        return invoice.id, payment.id


def _transaction(reference: str, *, amount_cents: int = 9900) -> dict[str, Any]:
    return {
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
        "created_at": "2026-08-29T11:33:44.592345",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }


async def _apply_in_own_session(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, transaction: dict[str, Any]
) -> str:
    """One callback, on a connection nobody else is using."""
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    signature = hmac_signature(transaction, secret=HMAC_SECRET)
    event = _provider().verify_callback(payload=body, signature=signature)

    async with committing() as session:
        service = CheckoutService(session, tenant_id=tenant_id, provider=_provider())
        outcome = await service.apply(event)
        await session.commit()
        return outcome


# ------------------------------------------------------------- the callback


async def test_four_simultaneous_deliveries_settle_the_invoice_once(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The retry storm, as it actually arrives.

    A provider that did not get a 2xx sends the same callback again, and
    "again" overlaps with the first attempt often enough that this is the
    normal case rather than the unlucky one. Exactly one delivery may settle;
    the rest must find the event already claimed.

    Four connections, four transactions, one row allowed to win.
    """
    tenant_id, _ = workspace
    invoice_id, payment_id = await _committed_invoice(committing, tenant_id)
    transaction = _transaction(str(payment_id))

    outcomes = await asyncio.gather(
        *(_apply_in_own_session(committing, tenant_id, transaction) for _ in range(4)),
        return_exceptions=True,
    )

    assert sorted(str(outcome) for outcome in outcomes) == [
        APPLIED,
        DUPLICATE,
        DUPLICATE,
        DUPLICATE,
    ]

    async with committing() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        assert invoice.status is InvoiceStatus.PAID
        # The figure is what proves it: settling twice would show 198.00.
        assert invoice.amount_paid == Decimal("99.00")

        events = await session.scalar(
            select(func.count())
            .select_from(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id)
        )
        assert events == 1


async def test_the_one_that_wins_is_the_one_that_records_the_work(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A duplicate must not overwrite the outcome the winner wrote.

    The claim is inserted before the decision is made, so a second delivery
    that got as far as the constraint has to stop there - not carry on and
    restate an outcome for an event it does not own.
    """
    tenant_id, _ = workspace
    _, payment_id = await _committed_invoice(committing, tenant_id)
    transaction = _transaction(str(payment_id))

    await asyncio.gather(
        *(_apply_in_own_session(committing, tenant_id, transaction) for _ in range(3))
    )

    async with committing() as session:
        event = (
            await session.execute(select(PaymentEvent).where(PaymentEvent.payment_id == payment_id))
        ).scalar_one()
        assert event.outcome == APPLIED
        assert event.processed_at is not None
        assert event.event_type == "transaction.succeeded"


# -------------------------------------------------------------- the checkout


async def test_two_checkouts_started_at_once_bill_the_period_once(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """`UNIQUE(tenant_id, period_start)` decides it, and the loser recovers.

    Two owners clicking at the same moment, or one double-clicking. Both may
    open a payment page - each attempt is its own row, which is what the
    payments table is for - but there must be exactly one invoice, or the
    workspace is billed twice for one month.

    The loser re-reads rather than raising: an integrity error surfacing as a
    500 to somebody who did nothing wrong is a bug, not a guarantee.
    """
    tenant_id, _ = workspace
    plan_code = await _plan_code(committing, workspace)

    async def start() -> uuid.UUID:
        async with committing() as session:
            service = CheckoutService(session, tenant_id=tenant_id, provider=_provider())
            started = await service.start(plan_code=plan_code)
            await session.commit()
            return started.invoice_id

    first, second = await asyncio.gather(start(), start())

    assert first == second
    async with committing() as session:
        invoices = await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == tenant_id)
        )
        payments = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant_id)
        )
    assert invoices == 1
    assert payments == 2


async def test_one_idempotency_key_opens_one_payment_page(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A retried request is recognised rather than duplicated.

    Refused rather than replayed, because the first response carried a one-use
    URL that is deliberately never stored - there is nothing honest to replay.
    The caller learns its first request was accepted and can read the payment's
    status, which is what it wanted to know.
    """
    tenant_id, _ = workspace
    plan_code = await _plan_code(committing, workspace)
    key = f"req-{uuid.uuid4().hex[:12]}"

    async def start() -> StartedCheckout | Exception:
        async with committing() as session:
            service = CheckoutService(session, tenant_id=tenant_id, provider=_provider())
            try:
                started = await service.start(plan_code=plan_code, idempotency_key=key)
            except Exception as error:
                return error
            await session.commit()
            return started

    outcomes = await asyncio.gather(start(), start())

    refused = [item for item in outcomes if isinstance(item, Exception)]
    assert len(refused) == 1, "exactly one of two identical requests must be refused"
    assert isinstance(refused[0], ConflictError)

    async with committing() as session:
        payments = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant_id)
        )
    assert payments == 1


async def test_different_keys_are_different_attempts(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Somebody who abandoned a page and started again gets a new one.

    The key deduplicates a retry of one request, not a customer changing their
    mind - and a customer whose second attempt was refused because their first
    timed out would have no way to pay at all.
    """
    tenant_id, _ = workspace
    plan_code = await _plan_code(committing, workspace)

    for index in range(2):
        async with committing() as session:
            service = CheckoutService(session, tenant_id=tenant_id, provider=_provider())
            await service.start(plan_code=plan_code, idempotency_key=f"attempt-{index}")
            await session.commit()

    async with committing() as session:
        payments = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant_id)
        )
        invoices = await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == tenant_id)
        )
    assert payments == 2
    assert invoices == 1


async def test_one_workspaces_key_does_not_block_anothers(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    prepared_database: str,
) -> None:
    """The constraint is per workspace, and that is a security property.

    A globally unique key would let one customer stop another from starting a
    checkout by guessing a string - a denial of service costing one request.
    """
    tenant_id, _ = workspace
    plan_code = await _plan_code(committing, workspace)
    key = "the-same-key"

    other_slug = f"conc-{uuid.uuid4().hex[:10]}"
    async with committing() as session:
        other = Tenant(name="Other", slug=other_slug)
        session.add(other)
        await session.commit()
        other_id = other.id

    try:
        for target in (tenant_id, other_id):
            async with committing() as session:
                service = CheckoutService(session, tenant_id=target, provider=_provider())
                await service.start(plan_code=plan_code, idempotency_key=key)
                await session.commit()

        async with committing() as session:
            payments = await session.scalar(
                select(func.count()).select_from(Payment).where(Payment.idempotency_key == key)
            )
        assert payments == 2
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == other_id))
            await session.commit()


async def _plan_code(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> str:
    _, plan_id = workspace
    async with committing() as session:
        plan = await session.get(Plan, plan_id)
        assert plan is not None
        return plan.code
