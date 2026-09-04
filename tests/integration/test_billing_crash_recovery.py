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
rather than a stronger lock (ADR-088).

What is asserted here is the provider's own call count. The payment table
cannot see this failure - it records one attempt in both the broken and the
fixed world - and a ledger that agrees with itself while disagreeing with the
bank is precisely the shape of the bug.

## The two drills

`test_a_crash_after_the_charge_does_not_charge_again` injects the failure by
losing the transaction that was open when the provider returned.
Deterministic, fast, runs in CI, and produces exactly the state a killed
process leaves behind.

`test_a_killed_worker_does_not_let_the_next_sweep_charge_again` kills a real
child process instead. Less precise about *where* it dies, and it is the only
version that proves no `finally`, no context manager and no rollback of ours is
what makes this safe - the same argument `test_media_crash_recovery.py` makes
for object writes.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.invoice import CollectionState, Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.checkout import SavedMethodCharge
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.fakes import as_database

pytestmark = pytest.mark.integration

CHILD = Path(__file__).with_name("interrupted_collection_child.py")
# Generous: what is being waited for is somebody else's Python starting up, and
# a timeout here is a failed test rather than a flaky assertion - the parent has
# nothing to assert until the child has spoken.
CHILD_TIMEOUT_SECONDS = 120

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
            # Audit rows first, while `tenant_id` still says whose they are.
            # That foreign key is `ON DELETE SET NULL` on purpose - a
            # privileged action survives the workspace it was performed on - so
            # a suite that commits for real and deletes only its tenants leaves
            # them behind for the next test to count. The dunning suites count
            # audit rows by action, and this file sweeps forty days into the
            # future, which produces exactly the ones they are looking for.
            await session.execute(delete(AuditLog).where(AuditLog.tenant_id == identifiers[0]))
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


async def test_a_crash_after_the_charge_does_not_charge_again(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE: one invoice, one crash, one charge.

    This is the assertion the reproduction inverted. Before ADR-088 two
    money-moving requests left here for one 100.00 EGP invoice under two
    different references, and the ledger recorded one of them.
    """
    tenant_id, _ = workspace
    sessions = Sessions(committing)
    sessions.die_after_charging = True
    provider = ChargeRecorder(sessions)
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    await _worker(sessions).run_once(now=NOW)
    assert sessions.died == 1, "the drill did not lose the transaction it meant to"
    assert len(provider.charges) == 1, "the sweep should have reached the provider once"

    # A replacement worker, on exactly the state the dead one left behind.
    await _worker(Sessions(committing)).run_once(now=NOW)

    assert (
        len(provider.charges) == 1
    ), f"the card was charged {len(provider.charges)} times for one invoice: {provider.charges}"
    assert AMOUNT * len(provider.charges) == AMOUNT, "one bill, one debit"

    rows = await _payments(committing, tenant_id)
    assert len(rows) == 1, "one logical attempt is one durable payment row"
    assert (
        str(rows[0].id) == provider.charges[0]
    ), "the reference Paymob was given must name a row that still exists"


async def test_the_attempt_is_committed_before_the_provider_call(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE: the ordering itself, observed from outside the worker's transaction.

    The provider double reads the payments table on a *separate* connection
    while the charge is in flight. A row it can see there is a row that is
    committed, because nothing else could make it visible to another
    transaction - which is the whole invariant: PostgreSQL knows the attempt
    exists before Paymob is asked to move money.
    """
    tenant_id, _ = workspace
    seen: list[tuple[str, str]] = []

    class Observing(ChargeRecorder):
        async def charge_saved_method(self, charge: SavedMethodCharge) -> str:
            async with committing() as watcher:
                rows = await watcher.execute(
                    select(Payment.id, Payment.collection_state).where(
                        Payment.tenant_id == tenant_id
                    )
                )
                seen.extend((str(pid), str(state)) for pid, state in rows.all())
            return await super().charge_saved_method(charge)

    provider = Observing()
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    await _worker(Sessions(committing)).run_once(now=NOW)

    assert len(provider.charges) == 1
    assert (
        len(seen) == 1
    ), "another transaction could not see the attempt while Paymob was being called"
    payment_id, state = seen[0]
    assert payment_id == provider.charges[0], "the committed row is the one Paymob was told about"
    assert (
        state == CollectionState.REQUESTED.value
    ), "'a charge may have been sent' has to be durable before one can be"


async def test_a_crashed_attempt_keeps_its_reference(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart does not invent a second name for one logical attempt.

    The audit's reproduction showed two charges under two different references,
    which is what makes a duplicate unattributable: neither Paymob nor Wasla
    can tell that the two describe one invoice.
    """
    tenant_id, _ = workspace
    sessions = Sessions(committing)
    sessions.die_after_charging = True
    provider = ChargeRecorder(sessions)
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    await _worker(sessions).run_once(now=NOW)

    rows = await _payments(committing, tenant_id)
    assert len(rows) == 1
    assert provider.charges == [str(rows[0].id)]
    assert rows[0].status is PaymentStatus.PENDING, "nobody knows what it did yet"
    assert rows[0].collection_state is CollectionState.REQUESTED

    invoice = await _invoice(committing, tenant_id)
    assert invoice.collection_attempts == 1, "the attempt the provider was told about was counted"
    assert invoice.status is InvoiceStatus.OPEN, "a request is not a settlement"


async def test_an_unresolved_attempt_blocks_the_next_charge(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE: the invoice stays uncollectible while the outcome is unknown.

    Not for one sweep - for every sweep, and however far past the retry
    schedule the clock has moved. A due date is not a licence to charge; the
    state of the last attempt decides.
    """
    tenant_id, _ = workspace
    sessions = Sessions(committing)
    sessions.die_after_charging = True
    monkeypatch.setattr(
        worker_module, "build_checkout_provider", lambda settings: ChargeRecorder(sessions)
    )
    await _worker(sessions).run_once(now=NOW)

    async with committing() as session:
        rows = await session.execute(select(Invoice).where(Invoice.tenant_id == tenant_id))
        invoice = rows.scalars().one()
        invoice.next_collection_at = NOW - timedelta(days=30)
        await session.commit()

    later = ChargeRecorder()
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: later)
    for _ in range(3):
        await _worker(Sessions(committing)).run_once(now=NOW + timedelta(days=40))

    assert later.charges == [], "a due date charged a card whose last outcome is unknown"
    assert len(await _payments(committing, tenant_id)) == 1


async def test_a_second_unresolved_attempt_cannot_be_written_at_all(
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE: the constraint, not the query.

    Everything above stops a second charge by asking first. This asserts what
    holds underneath when a future edit forgets to ask: a second unresolved
    attempt against one invoice is refused by PostgreSQL, not by a service.
    """
    tenant_id, _ = workspace
    sessions = Sessions(committing)
    sessions.die_after_charging = True
    monkeypatch.setattr(
        worker_module, "build_checkout_provider", lambda settings: ChargeRecorder(sessions)
    )
    await _worker(sessions).run_once(now=NOW)

    rows = await _payments(committing, tenant_id)
    assert len(rows) == 1
    existing = rows[0]

    with pytest.raises(IntegrityError):
        async with committing() as session:
            session.add(
                Payment(
                    tenant_id=tenant_id,
                    invoice_id=existing.invoice_id,
                    status=PaymentStatus.PENDING,
                    amount=AMOUNT,
                    currency="EGP",
                    provider="paymob",
                    is_automatic=True,
                    collection_state=CollectionState.CLAIMED,
                    idempotency_key=f"auto:{existing.invoice_id}:99",
                )
            )
            await session.commit()


# ------------------------------------------------------------ the real kill


async def test_a_killed_worker_does_not_let_the_next_sweep_charge_again(
    committing: async_sessionmaker[AsyncSession],
    prepared_database: str,
    workspace: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE: a real process is killed with the money already gone.

    Everything above injects the failure by losing a transaction, which is
    honest about the state it produces and tidier than the real thing about how
    it gets there. This one starts a real Python process, waits for it to say
    the charge has been made, and terminates it from outside - holding an open
    connection, mid-unit-of-work, with nothing to catch it. `kill()` is
    `TerminateProcess` on Windows and `SIGKILL` on POSIX, and neither is
    catchable.

    The child charges through a double that reports to *this* process over a
    socket, so the count of money-moving requests survives the process that
    made them. That is the point: once the child is gone, the only two things
    that know a charge happened are this test and PostgreSQL - and PostgreSQL
    knowing is the whole of ADR-088.
    """
    tenant_id, _ = workspace

    charges: list[str] = []
    ready = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # One line per charge: the reference. The smallest protocol that
        # answers the only question - how many times did money move, and for
        # which attempt.
        raw = await reader.readline()
        charges.append(raw.decode().strip())
        ready.set()
        writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]

    root = Path(__file__).parents[2]
    environment = dict(os.environ)
    environment.update(
        {
            "WASLA_CHILD_DATABASE_URL": prepared_database,
            "WASLA_CHILD_TENANT_ID": str(tenant_id),
            "WASLA_CHILD_CHARGE_PORT": str(port),
            # A script's `sys.path[0]` is its own directory, so without this the
            # child imports whichever `app` an editable install points at -
            # which in a worktree is a different checkout entirely.
            "PYTHONPATH": str(root),
        }
    )

    async with server:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CHILD),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(root),
        )
        try:
            async with asyncio.timeout(CHILD_TIMEOUT_SECONDS):
                await ready.wait()
            # Killed here: the charge has been made and nothing has recorded
            # its outcome.
            child.kill()
        except TimeoutError:
            child.kill()
            stderr = b"" if child.stderr is None else await child.stderr.read()
            pytest.fail(f"the child never charged: {stderr.decode(errors='replace')}")
        finally:
            await child.wait()

    assert child.returncode != 0, "the child was supposed to be killed, not to exit"
    assert len(charges) == 1

    # What the dead worker left behind is enough to say a charge may have
    # happened.
    rows = await _payments(committing, tenant_id)
    assert len(rows) == 1, "the attempt outlived the process that made it"
    assert str(rows[0].id) == charges[0]
    assert rows[0].collection_state is CollectionState.REQUESTED

    # And a replacement worker does not send another.
    provider = ChargeRecorder()
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)
    await _worker(Sessions(committing)).run_once(now=NOW)

    assert (
        provider.charges == []
    ), "a replacement worker charged a card whose outcome is still unknown"
    assert len(await _payments(committing, tenant_id)) == 1
