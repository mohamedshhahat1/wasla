"""A billing worker dies with the card already charged, and nothing charges it again.

This is WSL-01, the one confirmed financial defect the repository audit found,
and the file exists because the billing suites had no test of this shape. They
cover two workers racing for one invoice and a provider refusing a charge.
Neither is this. This is **one** worker, with nobody to race, that stops
existing between the money moving and the record of it becoming durable.

The original failure, reproduced against real PostgreSQL before anything was
changed:

    sweep 1  claim invoice -> INSERT payment -> attempts += 1
             -> POST Paymob  (money moves)
             -> process dies
             -> PostgreSQL rolls back the payment row and the counter

    sweep 2  reads an invoice with collection_attempts = 0 and status = OPEN,
             builds the same idempotency key - which no longer collides,
             because the row that carried it was rolled back
             -> POST Paymob  (money moves again)

    result   100.00 EGP invoice, 200.00 EGP taken, one payment row

`SKIP LOCKED` does not help: a lock is held by a process, and this process is
gone. The idempotency key does not help either: it lived on the row that was
rolled back. Nothing that is only written inside the transaction can survive
the transaction not committing, which is why the fix is an ordering change
rather than a stronger lock.

What is asserted here is the provider's own call count. The payment table
cannot see this failure - it records one attempt in both the broken and the
fixed world - and a ledger that agrees with itself while disagreeing with the
bank is precisely the shape of the bug.

## Where this file is in its life

Right now it holds the **reproduction**: assertions that describe the defect,
so that a fix has something to invert. That is deliberate and temporary. When
the durable collection protocol lands, every number here changes and this file
becomes the regression suite that keeps it closed.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.invoice import Invoice, InvoiceStatus, Payment
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.checkout import SavedMethodCharge
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.fakes import as_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
PERIOD_START = datetime(2026, 8, 1, tzinfo=UTC)
AMOUNT = Decimal("100.00")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "jwt_secret": secrets.token_urlsafe(32),
    }
    values.update(overrides)
    return Settings(**values)


class Sessions:
    """Hands out real, committing sessions, and can lose one on request.

    A worker opens a session per claim, so this must give out a new one each
    time. `die_after_charging` is the injection: the transaction that was open
    when the provider was called is rolled back instead of committed, which is
    what PostgreSQL does for a transaction whose client has stopped existing.
    An aborted transaction and a deliberately rolled-back one are the same
    thing to the database, which is what makes this a reproduction rather than
    an approximation.

    Marking the *charging* transaction rather than the first or the second one
    matters: a sweep opens several before it reaches the provider - the
    roll-over phase, then the claim batch - and crashing one of those would
    prove nothing about money.
    """

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self.die_after_charging = False
        self.died = 0
        self._doomed = False

    def charged(self) -> None:
        """Called by the provider double from inside the worker's transaction."""
        if self.die_after_charging:
            self._doomed = True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[object]:
        session = self._maker()
        try:
            yield session
            if self._doomed:
                self._doomed = False
                self.die_after_charging = False
                self.died += 1
                await session.rollback()
            else:
                await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class ChargeRecorder:
    """A provider double that remembers every charge it was asked to make.

    The count is the whole assertion. A duplicate charge is visible here and
    nowhere else in the system: by the time the payment table could show it,
    the money has already left the customer's account.
    """

    name = "paymob"

    def __init__(self, sessions: Sessions | None = None) -> None:
        self.charges: list[str] = []
        self._sessions = sessions

    @property
    def can_charge_saved_methods(self) -> bool:
        return True

    def verify_token_callback(self, *, payload: bytes, signature: str | None) -> object:
        raise NotImplementedError("no saved-card callback arrives in this test")

    async def charge_saved_method(self, charge: SavedMethodCharge) -> str:
        self.charges.append(charge.reference)
        if self._sessions is not None:
            self._sessions.charged()
        return f"txn-{len(self.charges)}"


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, over an engine of this test's own.

    A rolled-back fixture transaction could not host this: the failure being
    reproduced is a transaction that does not commit, and a suite in which
    nothing commits cannot tell that from one that does.
    """
    engine = create_async_engine(prepared_database, pool_size=6, max_overflow=4)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """One workspace with a saved card and one unpaid renewal. Removed afterwards.

    Deleting the tenant cascades to the subscription, the invoice, the payments
    and the card. The plan is global and goes separately.
    """
    async with committing() as session:
        tenant = Tenant(name="Crash", slug=f"crash-{uuid.uuid4().hex[:10]}")
        plan = Plan(
            code=f"crash-{uuid.uuid4().hex[:8]}",
            name="Crash",
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
            status=SubscriptionStatus.ACTIVE,
            current_period_start=PERIOD_START,
            current_period_end=NOW + timedelta(days=25),
        )
        session.add(subscription)
        await session.flush()

        session.add(
            Invoice(
                tenant_id=tenant.id,
                subscription_id=subscription.id,
                status=InvoiceStatus.OPEN,
                plan_code=plan.code,
                amount_due=AMOUNT,
                amount_paid=Decimal("0.00"),
                currency="EGP",
                period_start=PERIOD_START,
                period_end=PERIOD_START + timedelta(days=30),
                # Yesterday, so the dunning phases are not eligible and the
                # sweep's return value counts collections and nothing else.
                issued_at=NOW - timedelta(days=1),
                lines=[],
            )
        )
        session.add(
            PaymentMethod(
                tenant_id=tenant.id,
                provider="paymob",
                provider_token=f"tok-{uuid.uuid4().hex[:12]}",
                provider_token_id="15978654",
                masked_pan="**** 2346",
                brand="MasterCard",
                status=PaymentMethodStatus.ACTIVE,
                is_default=True,
            )
        )
        await session.commit()
        identifiers = (tenant.id, plan.id)

    try:
        yield identifiers
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == identifiers[0]))
            await session.execute(delete(Plan).where(Plan.id == identifiers[1]))
            await session.commit()


def _worker(handle: object) -> BillingWorker:
    return BillingWorker(database=as_database(handle), settings=_settings(), claim_limit=10)


async def _payments(
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> list[Payment]:
    async with committing() as session:
        rows = await session.execute(
            select(Payment).where(Payment.tenant_id == tenant_id).order_by(Payment.created_at)
        )
        return list(rows.scalars())


async def _invoice(
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> Invoice:
    async with committing() as session:
        row = await session.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
        return row.scalars().one()


# ------------------------------------------------------- the injected crash


async def test_a_crash_after_the_charge_charges_the_card_again(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect, executed. **These assertions are the bug, not the contract.**

    They say what this code does today so that the fix has something to
    invert, in the same way `test_media_write_atomicity.py` was written
    against the orphan before ADR-087 closed it. When the collection protocol
    commits its attempt before reaching Paymob, every number below changes and
    this test is rewritten to demand the opposite.

    Two charges leave here for one 100.00 EGP invoice, under two different
    references, both calling themselves attempt 1 - and the payment table ends
    up recording one of them. That last part is what makes this expensive to
    find in production: nothing in Wasla's own data disagrees with itself. The
    only place the second charge is visible is the customer's statement.
    """
    tenant_id, _ = workspace
    sessions = Sessions(committing)
    sessions.die_after_charging = True
    provider = ChargeRecorder(sessions)
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    await _worker(sessions).run_once(now=NOW)

    assert sessions.died == 1, "the drill did not lose the transaction it meant to"

    # The reproduction. Two money-moving requests for one invoice.
    assert len(provider.charges) == 2, "WSL-01 did not reproduce"
    assert len(set(provider.charges)) == 2, "and under two different references"

    rows = await _payments(committing, tenant_id)
    assert len(rows) == 1, "the ledger records one of the two"
    assert str(rows[0].id) == provider.charges[1], "the first charge left no row at all"

    invoice = await _invoice(committing, tenant_id)
    assert invoice.collection_attempts == 1, "and one attempt, having made two"
    assert invoice.status is InvoiceStatus.OPEN

    # Stated in the units that matter. The card was debited twice for a bill
    # that was issued once.
    assert AMOUNT * len(provider.charges) == Decimal("200.00")
    assert invoice.amount_due == AMOUNT
