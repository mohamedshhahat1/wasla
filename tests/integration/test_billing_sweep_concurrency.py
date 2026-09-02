"""Two billing workers, sweeping at once, against a real database.

The sweep used to be one transaction taking one batch of `CLAIM_LIMIT` rows with
no row locks at all. A second replica was not a way to go faster, it was a way
to bill somebody twice: both workers read the same due subscriptions, both
issued the same invoice, and the loser found out from an integrity error that
aborted its whole pass. Correctness rested on unique constraints, and one of
them - `uq_invoices_tenant_id_period_start` - fired *after* the work rather
than instead of it (ADR-082).

This file is the executed proof that two workers now divide the work. Like
`test_billing_concurrency.py`, it opens its own engine and commits for real,
because a claim is a row lock and two coroutines sharing a transaction cannot
race for one: they would both hold it and every assertion here would pass on
the broken code.

What is asserted is structural, not timing. "Both workers did some of the work"
and "each side effect happened exactly once" are facts about rows; how long a
sweep took on this machine is not evidence of anything.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.email import OutboundEmail
from app.db.models.invoice import Invoice, InvoiceStatus, Payment
from app.db.models.membership import Membership, TenantRole
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.checkout import SavedMethodCharge
from app.repositories.invoice_repository import PlatformInvoiceRepository
from app.services.recurring_service import MAX_COLLECTION_ATTEMPTS
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PERIOD_START = datetime(2026, 8, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 9, 1, tzinfo=UTC)
COHORT = 7
CLAIM_LIMIT = 2


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "jwt_secret": secrets.token_urlsafe(32),
        # On, and faked: the notices are what could double under concurrency,
        # so a deployment with email off would assert nothing here.
        "email_enabled": True,
        "email_provider": "fake",
        "email_from": "no-reply@wasla.test",
    }
    values.update(overrides)
    return Settings(**values)


class PooledHandle:
    """A stand-in for `Database` that hands out real, committing sessions.

    The worker opens a session per claim, so this must give out a *new* one
    each time - the shared-session handle the other worker tests use would
    collapse two workers into one transaction and there would be no race left
    to lose.
    """

    def __init__(self, maker: async_sessionmaker) -> None:
        self._maker = maker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[object]:
        session = self._maker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class CountingProvider:
    """A recurring provider that records every charge it is asked for.

    The count is the assertion: two workers sweeping the same collectible
    invoice must produce one request to Paymob, not two, and a duplicate caught
    afterwards by a unique key is a duplicate that already moved money.
    """

    name = "paymob"

    def __init__(self, *, fail_for: set[uuid.UUID] | None = None) -> None:
        self.charges: list[str] = []
        self._fail_for = fail_for or set()
        self._lock = asyncio.Lock()

    @property
    def can_charge_saved_methods(self) -> bool:
        return True

    def verify_token_callback(self, *, payload: bytes, signature: str | None) -> object:
        # Part of `RecurringProvider`, and the worker's `isinstance` check is
        # structural - so a fake missing it is silently not a provider at all
        # and the collection phase returns zero without saying why.
        raise NotImplementedError("no saved-card callback arrives in this test")

    async def charge_saved_method(self, charge: SavedMethodCharge) -> str:
        async with self._lock:
            self.charges.append(charge.reference)
        if uuid.UUID(charge.reference) in self._fail_for:
            raise RuntimeError("the provider fell over for this one")
        # Yield, so a second worker gets a chance to interleave here rather
        # than the whole charge running as one uninterrupted step.
        await asyncio.sleep(0)
        return f"intent-{len(self.charges)}"


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker]:
    """Sessions that really commit, over an engine of this test's own.

    A real pool rather than `NullPool`: each worker takes a session per claim
    and two workers run at once, so this needs several connections and wants to
    reuse them.
    """
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def plan(committing) -> AsyncIterator[uuid.UUID]:
    code = f"sweep-{uuid.uuid4().hex[:10]}"
    async with committing() as session:
        row = Plan(
            code=code,
            name="Sweep",
            price=Decimal("99.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.AGENTS.value: 5},
        )
        session.add(row)
        await session.commit()
        plan_id = row.id
    try:
        yield plan_id
    finally:
        async with committing() as session:
            await session.execute(delete(Plan).where(Plan.id == plan_id))
            await session.commit()


@pytest_asyncio.fixture
async def tenants(committing) -> AsyncIterator[list[uuid.UUID]]:
    """A committed cohort, removed afterwards - the delete cascades."""
    created: list[uuid.UUID] = []
    async with committing() as session:
        for index in range(COHORT):
            tenant = Tenant(name=f"Sweep {index}", slug=f"sweep-{uuid.uuid4().hex[:10]}")
            session.add(tenant)
            await session.flush()
            created.append(tenant.id)
        await session.commit()
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id.in_(created)))
            await session.commit()


async def _subscribe(
    committing,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID,
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    end: datetime = PERIOD_END,
) -> uuid.UUID:
    async with committing() as session:
        subscription = Subscription(
            tenant_id=tenant_id,
            plan_id=plan_id,
            status=status,
            current_period_start=PERIOD_START,
            current_period_end=end,
        )
        session.add(subscription)
        await session.commit()
        return subscription.id


async def _open_invoice(
    committing,
    tenant_id: uuid.UUID,
    subscription_id: uuid.UUID,
    *,
    period_start: datetime,
    issued_at: datetime,
) -> uuid.UUID:
    async with committing() as session:
        invoice = Invoice(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            status=InvoiceStatus.OPEN,
            plan_code="sweep",
            amount_due=Decimal("99.00"),
            amount_paid=Decimal("0.00"),
            currency="EGP",
            period_start=period_start,
            period_end=period_start + timedelta(days=30),
            lines=[],
            issued_at=issued_at,
        )
        session.add(invoice)
        await session.commit()
        return invoice.id


async def _owner(committing, tenant_id: uuid.UUID) -> None:
    """Somebody for a notice to be addressed to.

    `EmailOutbox.enqueue_for_tenant_owners` writes one row per *active owner*,
    so a workspace with no membership produces no mail however correct the
    transition is - and a test counting notices would then assert nothing.
    """
    async with committing() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password="not-a-real-hash",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user.id, role=TenantRole.TENANT_OWNER))
        await session.commit()


async def _card(committing, tenant_id: uuid.UUID) -> None:
    async with committing() as session:
        session.add(
            PaymentMethod(
                tenant_id=tenant_id,
                status=PaymentMethodStatus.ACTIVE,
                is_default=True,
                provider="paymob",
                provider_token=f"tok-{uuid.uuid4().hex[:12]}",
                provider_token_id="15978654",
                brand="MasterCard",
                masked_pan="**** 2346",
            )
        )
        await session.commit()


def _worker(committing, **overrides: object) -> BillingWorker:
    return BillingWorker(
        database=PooledHandle(committing),
        settings=_settings(**overrides),
        claim_limit=CLAIM_LIMIT,
    )


async def _count(committing, statement) -> int:
    async with committing() as session:
        return int(await session.scalar(statement) or 0)


# ------------------------------------------------------------ the roll-over


async def test_two_workers_advance_every_subscription_exactly_once(
    committing,
    plan,
    tenants,
):
    """GATE: a cohort divided between two workers, each row done once.

    Seven subscriptions, a claim limit of two, two workers. Every one is
    advanced, exactly one invoice exists per workspace for the period that
    ended, and the two workers between them did all seven - which is only
    possible if each claim excluded the other worker.
    """
    for tenant_id in tenants:
        await _subscribe(committing, tenant_id, plan)

    first, second = _worker(committing), _worker(committing)
    handled = await asyncio.gather(
        first.run_once(now=NOW),
        second.run_once(now=NOW),
    )

    assert sum(handled) == COHORT, f"workers reported {handled}"
    invoices = await _count(
        committing,
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.tenant_id.in_(tenants))
        .where(Invoice.period_start == PERIOD_START),
    )
    assert invoices == COHORT

    async with committing() as session:
        rolled = (
            await session.execute(
                select(Subscription.current_period_start).where(Subscription.tenant_id.in_(tenants))
            )
        ).scalars()
        # Every period moved on, and moved on once.
        assert set(rolled) == {PERIOD_END}


async def test_neither_worker_bills_the_same_period_twice(committing, plan, tenants):
    """The unique key is still there; this asserts it is never reached.

    An `IntegrityError` on `uq_invoices_tenant_id_period_start` is what used to
    happen, and it aborted the losing worker's whole transaction. Counting
    invoices proves there is one; the workers both reporting success proves
    neither of them lost a pass discovering it.
    """
    for tenant_id in tenants:
        await _subscribe(committing, tenant_id, plan)

    workers = [_worker(committing) for _ in range(2)]
    handled = await asyncio.gather(*(w.run_once(now=NOW) for w in workers))

    assert sum(handled) == COHORT
    per_tenant = await _count(
        committing,
        select(func.count()).select_from(
            select(Invoice.tenant_id)
            .where(Invoice.tenant_id.in_(tenants))
            .group_by(Invoice.tenant_id)
            .having(func.count() > 1)
            .subquery()
        ),
    )
    assert per_tenant == 0, "a workspace was billed twice for one period"


async def test_a_row_another_worker_holds_is_skipped_rather_than_waited_for(
    committing,
    plan,
    tenants,
):
    """SKIP LOCKED, demonstrated as a skip rather than as a wait.

    One subscription is locked by a transaction this test holds open for the
    whole sweep. A worker that waited would block until the lock was released;
    a worker that skips does the other six and finishes. The held row is then
    untouched, and a later sweep picks it up.
    """
    subscriptions = [await _subscribe(committing, tenant_id, plan) for tenant_id in tenants]
    held = subscriptions[0]

    holder = committing()
    try:
        locked = (
            await holder.execute(
                select(Subscription).where(Subscription.id == held).with_for_update()
            )
        ).scalar_one()
        assert locked is not None

        # A short timeout, because "it skipped" and "it is still waiting" are
        # the two outcomes and only one of them finishes.
        async with asyncio.timeout(30):
            handled = await _worker(committing).run_once(now=NOW)
    finally:
        await holder.rollback()
        await holder.close()

    assert handled == COHORT - 1
    async with committing() as session:
        still_due = await session.scalar(
            select(Subscription.current_period_start).where(Subscription.id == held)
        )
    assert still_due == PERIOD_START, "the held row should have been left alone"

    # And the next sweep, with nobody holding it, finishes the job.
    assert await _worker(committing).run_once(now=NOW) == 1


# ------------------------------------------------------------- the collection


async def test_two_workers_make_one_collection_attempt_per_invoice(
    committing,
    plan,
    tenants,
    monkeypatch,
):
    """GATE: no duplicate Paymob charge, counted at the provider.

    The payment row's `UNIQUE(tenant_id, idempotency_key)` would catch a second
    attempt, but only after the request had been made - and a request that was
    made is money that moved. So the assertion is on the provider's own call
    count, which is the only place a duplicate charge is visible before it
    happens.
    """
    provider = CountingProvider()
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    invoices = []
    for tenant_id in tenants:
        subscription_id = await _subscribe(
            committing,
            tenant_id,
            plan,
            end=PERIOD_END + timedelta(days=30),
        )
        await _card(committing, tenant_id)
        invoices.append(
            await _open_invoice(
                committing,
                tenant_id,
                subscription_id,
                period_start=PERIOD_START,
                # Issued yesterday, so the grace period has not run and the
                # dunning phases are not eligible. Otherwise `handled` counts
                # collections, chases and suspensions together and says
                # nothing about any of them.
                issued_at=NOW - timedelta(days=1),
            )
        )

    handled = await asyncio.gather(
        _worker(committing).run_once(now=NOW),
        _worker(committing).run_once(now=NOW),
    )

    # The provider's own count, which is the only place a duplicate charge is
    # visible before the money has moved.
    assert len(provider.charges) == COHORT
    assert len(set(provider.charges)) == COHORT, "one payment was charged twice"
    assert sum(handled) == COHORT
    assert min(handled) > 0, f"one worker did nothing: {handled}"

    payments = await _count(
        committing,
        select(func.count()).select_from(Payment).where(Payment.tenant_id.in_(tenants)),
    )
    assert payments == COHORT

    async with committing() as session:
        attempts = (
            await session.execute(
                select(Invoice.collection_attempts).where(Invoice.id.in_(invoices))
            )
        ).scalars()
        assert set(attempts) == {1}


async def test_one_workspaces_provider_failure_does_not_strand_the_others(
    committing,
    plan,
    tenants,
    monkeypatch,
):
    """A failure is contained to its own claim, not to the pass.

    This is what the per-claim transaction buys. Under one transaction per
    sweep, an exception escaping a collection took every other workspace's
    committed work with it.
    """
    for tenant_id in tenants:
        subscription_id = await _subscribe(
            committing,
            tenant_id,
            plan,
            end=PERIOD_END + timedelta(days=30),
        )
        await _card(committing, tenant_id)
        await _open_invoice(
            committing,
            tenant_id,
            subscription_id,
            period_start=PERIOD_START,
            issued_at=NOW - timedelta(days=1),
        )

    # Which invoice fails is not knowable in advance - the charge reference is
    # the payment id, and that row does not exist until it is claimed - so the
    # provider refuses whichever charge reaches it first. One failure is one
    # failure whoever it lands on.
    provider = CountingProvider()
    original = provider.charge_saved_method

    async def fail_first(charge: SavedMethodCharge) -> str:
        if not provider.charges:
            provider.charges.append(charge.reference)
            raise RuntimeError("the provider fell over for this one")
        return await original(charge)

    provider.charge_saved_method = fail_first  # type: ignore[method-assign]
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: provider)

    handled = await asyncio.gather(
        _worker(committing).run_once(now=NOW),
        _worker(committing).run_once(now=NOW),
    )

    # Six of seven collected. The seventh is not lost and not retried in this
    # pass: its attempt row is committed with no provider reference, which is
    # what `_claim_attempt` means by counting an attempt before making it - a
    # request whose outcome is unknown has still been made, and a scheme counts
    # it. That is the same conservative direction the uncertain-send
    # quarantine takes with a WhatsApp message.
    assert len(provider.charges) == COHORT
    assert sum(handled) == COHORT - 1

    attempts = await _count(
        committing,
        select(func.count()).select_from(Payment).where(Payment.tenant_id.in_(tenants)),
    )
    assert attempts == COHORT, "the failed attempt should still be on the record"

    referenced = await _count(
        committing,
        select(func.count())
        .select_from(Payment)
        .where(Payment.tenant_id.in_(tenants))
        .where(Payment.provider_intent_reference.is_not(None)),
    )
    assert referenced == COHORT - 1, "only the six that answered should carry a reference"


# ---------------------------------------------------------------- the dunning


async def test_two_overdue_invoices_of_one_workspace_produce_one_transition(
    committing,
    plan,
    tenants,
):
    """GATE: dunning does not duplicate under concurrency.

    A workspace with two overdue invoices is the case a per-invoice claim gets
    wrong: two workers take one invoice each, and both transition the same
    subscription. So the subscription is claimed alongside the invoice, and this
    asserts the three things that would otherwise double - the status change,
    the audit row and the notice.
    """
    tenant_id = tenants[0]
    await _owner(committing, tenant_id)
    subscription_id = await _subscribe(
        committing,
        tenant_id,
        plan,
        end=PERIOD_END + timedelta(days=30),
    )
    long_ago = NOW - timedelta(days=60)
    for offset in range(2):
        await _open_invoice(
            committing,
            tenant_id,
            subscription_id,
            period_start=long_ago + timedelta(days=offset),
            issued_at=long_ago + timedelta(days=offset),
        )

    await asyncio.gather(
        _worker(committing, billing_past_due_days=7, billing_suspend_after_days=90).run_once(
            now=NOW
        ),
        _worker(committing, billing_past_due_days=7, billing_suspend_after_days=90).run_once(
            now=NOW
        ),
    )

    async with committing() as session:
        status = await session.scalar(
            select(Subscription.status).where(Subscription.id == subscription_id)
        )
    assert status is SubscriptionStatus.PAST_DUE

    audits = await _count(
        committing,
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.action == AuditAction.SUBSCRIPTION_PAST_DUE),
    )
    assert audits == 1, "the transition was recorded twice"

    notices = await _count(
        committing,
        select(func.count()).select_from(OutboundEmail).where(OutboundEmail.tenant_id == tenant_id),
    )
    assert notices == 1, "the customer was told twice"


async def test_more_overdue_workspaces_than_the_claim_limit_all_get_chased(
    committing,
    plan,
    tenants,
):
    """The starvation fix, asserted.

    Chasing an invoice leaves it exactly as overdue as it was - only its
    subscription changes - so with the status checked after the claim the first
    `claim_limit` invoices held the front of every batch for ever and the ones
    behind them were never reached. The subscription state is now in the query,
    so a chased invoice leaves the eligible set.
    """
    long_ago = NOW - timedelta(days=60)
    for tenant_id in tenants:
        subscription_id = await _subscribe(
            committing,
            tenant_id,
            plan,
            end=PERIOD_END + timedelta(days=30),
        )
        await _open_invoice(
            committing,
            tenant_id,
            subscription_id,
            period_start=long_ago,
            issued_at=long_ago,
        )

    await _worker(committing, billing_past_due_days=7, billing_suspend_after_days=90).run_once(
        now=NOW
    )

    behind = await _count(
        committing,
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.tenant_id.in_(tenants))
        .where(Subscription.status == SubscriptionStatus.PAST_DUE),
    )
    assert behind == COHORT, f"only {behind} of {COHORT} were chased"


async def test_a_spent_collection_budget_stops_taking_a_slot(committing, plan, tenants):
    """The other half of the starvation fix.

    An invoice whose attempts are spent has `next_collection_at IS NULL`, which
    is the same state as one nobody has tried - so it stayed eligible for ever
    and occupied a place in every batch. The attempt budget is now in the claim
    query, so it drops out.
    """
    tenant_id = tenants[0]
    subscription_id = await _subscribe(
        committing,
        tenant_id,
        plan,
        end=PERIOD_END + timedelta(days=30),
    )
    invoice_id = await _open_invoice(
        committing,
        tenant_id,
        subscription_id,
        period_start=PERIOD_START,
        issued_at=PERIOD_START,
    )
    async with committing() as session:
        invoice = await session.get(Invoice, invoice_id)
        assert invoice is not None
        invoice.collection_attempts = MAX_COLLECTION_ATTEMPTS
        invoice.next_collection_at = None
        await session.commit()

    async with committing() as session:
        claimed = await PlatformInvoiceRepository(session).claim_collectible(
            before=NOW,
            max_attempts=MAX_COLLECTION_ATTEMPTS,
        )
        assert invoice_id not in {invoice.id for invoice in claimed}
