"""What happens when the provider keeps refusing to accept the request at all.

`_abandon` closes an attempt that provably never reached the processor, and
gives the attempt budget back - correctly, because a request that was never
sent must not spend a chance to charge somebody's card. What it also used to do
was clear `next_collection_at`, which made the invoice eligible again *at once*.

Both halves of that are defensible on their own and the combination is a loop:
the branch that declines to spend the budget is the branch that also clears the
schedule, so `MAX_COLLECTION_ATTEMPTS` can never engage. A persistent cause -
a merchant account with no Moto integration, a provider that will not accept
the intention - produced one `FAILED` payment row per invoice per billing poll,
for ever, with the attempt count returning to zero every time.

These tests drive real sweeps against real PostgreSQL with a provider double
that refuses deterministically, and count the rows. The interesting number is
not "did it fail" - it always fails - but **how many times it was allowed to
fail per unit of wall-clock time**.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider
from app.services.recurring_service import (
    ABANDON_BACKOFF,
    ATTEMPTS_EXHAUSTED,
    MAX_COLLECTION_ATTEMPTS,
    NOT_DUE,
    NOT_SENT,
    NOT_SUPPORTED,
    PROVIDER_REFUSED,
    RecurringService,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
MOTO_INTEGRATION = 9900001
CARD_TOKEN = "3f22ce8a4e77125c70f0bc69830e34c36df469351e2fa6be76428be4"
# One billing poll. The worker's sweep interval is minutes, and the point of
# these tests is what a *poll* is allowed to produce, so the clock advances by
# one of them between sweeps rather than by a day.
POLL = timedelta(minutes=5)


class Calls:
    """How many times the provider was actually asked for anything."""

    def __init__(self) -> None:
        self.intentions = 0
        self.pays = 0

    @property
    def total(self) -> int:
        return self.intentions + self.pays


def _transport(calls: Calls, *, intention_status: int = 201) -> httpx.MockTransport:
    """Paymob's two-step charge, with the *first* step refusing.

    Refusing the intention is what makes this a not-sent failure rather than a
    decline: creating an intention describes a payment, the pay request takes
    one, and only the second can move money. A `pays` count above zero in any
    of these tests would mean the double is not reproducing the condition.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "intention" in str(request.url):
            calls.intentions += 1
            if intention_status != 201:
                return httpx.Response(intention_status, json={"detail": "refused"})
            return httpx.Response(
                201,
                json={
                    "id": "pi_auto_1",
                    "client_secret": "csk_auto_1",
                    "payment_keys": [{"key": "a-payment-token", "integration": MOTO_INTEGRATION}],
                },
            )
        calls.pays += 1
        return httpx.Response(200, json={"id": 700000123, "pending": False, "success": True})

    return httpx.MockTransport(handler)


def _provider(
    transport: httpx.MockTransport,
    *,
    moto: int | None = MOTO_INTEGRATION,
) -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret="a-test-hmac-secret",
        integration_ids=[4097558],
        moto_integration_id=moto,
        transport=transport,
    )


def _service(
    session: AsyncSession,
    tenant: Tenant,
    *,
    transport: httpx.MockTransport,
    moto: int | None = MOTO_INTEGRATION,
) -> RecurringService:
    return RecurringService(
        session,
        tenant_id=tenant.id,
        provider=_provider(transport, moto=moto),
    )


class _NoMotoProvider(PaymobProvider):
    """Says it can charge saved cards, and then cannot.

    The exact shape of `RecurringUnavailableError`: `_refusal` asks
    `can_charge_saved_methods` *before* claiming an attempt, so a provider that
    answers False never reaches `_abandon` at all. Reaching it requires an
    account whose capability is discovered to be missing only once the charge
    is attempted - a merchant whose Moto integration was removed, or one whose
    configuration says one thing and whose account says another.
    """

    @property
    def can_charge_saved_methods(self) -> bool:
        return True


async def _workspace(
    session: AsyncSession,
    *,
    slug: str = "acme",
) -> tuple[Tenant, Subscription, Invoice]:
    """A workspace mid-period with an unpaid renewal and a saved card."""
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    plan = Plan(
        code=f"pro-{uuid.uuid4().hex[:6]}",
        name="Pro",
        price=Decimal("25.00"),
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
        current_period_start=NOW - timedelta(days=5),
        current_period_end=NOW + timedelta(days=25),
    )
    session.add(subscription)
    await session.flush()

    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        plan_code=plan.code,
        amount_due=Decimal("25.00"),
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=NOW - timedelta(days=35),
        period_end=NOW - timedelta(days=5),
        issued_at=NOW - timedelta(days=5),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    session.add(
        PaymentMethod(
            tenant_id=tenant.id,
            provider="paymob",
            provider_token=f"{CARD_TOKEN}-{uuid.uuid4().hex[:6]}",
            provider_token_id="15978654",
            masked_pan="xxxx-xxxx-xxxx-2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    await session.flush()
    return tenant, subscription, invoice


async def _payment_rows(session: AsyncSession, invoice: Invoice) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Payment.id)).where(Payment.invoice_id == invoice.id)
            )
        ).scalar_one()
    )


async def _sweep(
    session: AsyncSession,
    tenant: Tenant,
    subscription: Subscription,
    invoice: Invoice,
    *,
    transport: httpx.MockTransport,
    now: datetime,
    moto: int | None = MOTO_INTEGRATION,
    provider: PaymobProvider | None = None,
) -> str | None:
    """One billing poll, as `BillingWorker` performs it."""
    service = (
        RecurringService(session, tenant_id=tenant.id, provider=provider)
        if provider is not None
        else _service(session, tenant, transport=transport, moto=moto)
    )
    outcome = await service.collect(invoice, subscription=subscription, now=now)
    await session.refresh(invoice)
    return outcome.reason


# ------------------------------------------------------------- the loop


async def test_a_persistent_not_sent_failure_does_not_run_once_per_poll(
    db_session: AsyncSession,
) -> None:
    """F-2. The invoice stops being eligible on the very next poll.

    Measured against the pre-fix code, eight five-minute polls left:

        collection_attempts 0 · next_collection_at None · eligible every poll

    Eligible every poll is the defect. What it produced was not a row per poll -
    `UNIQUE(tenant_id, idempotency_key)` on the abandoned attempt's own key
    stopped that, and is also what made the invoice permanently uncollectible
    (see `test_an_invoice_recovers_once_the_cause_is_fixed`) - but the sweep
    re-ran the eligibility checks, re-read the card, and attempted an insert
    that rolled back, on every pass, for ever.

    Now the backoff decides. Eight polls spanning thirty-five minutes cross
    only the first fifteen-minute wait, so there are two attempts rather than
    eight passes: the schedule is what bounds the work, not a constraint
    failing.

    The attempt budget still comes back, because nothing was sent. What does
    not come back is the right to try again immediately.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=400)
    reasons = []

    for poll in range(8):
        reasons.append(
            await _sweep(
                db_session,
                tenant,
                subscription,
                invoice,
                transport=transport,
                now=NOW + POLL * poll,
            )
        )

    assert calls.pays == 0, "no money-moving request may have been made"
    # t=0 abandons and waits 15 minutes; t=15 abandons again and waits an hour.
    # Everything else in the thirty-five minutes is refused as not due.
    assert reasons == [
        PROVIDER_REFUSED,
        NOT_DUE,
        NOT_DUE,
        PROVIDER_REFUSED,
        NOT_DUE,
        NOT_DUE,
        NOT_DUE,
        NOT_DUE,
    ]
    assert await _payment_rows(db_session, invoice) == 2
    assert invoice.collection_attempts == 0
    assert invoice.next_collection_at == NOW + ABANDON_BACKOFF[0] + ABANDON_BACKOFF[1]


async def test_the_attempt_budget_is_still_returned(db_session: AsyncSession) -> None:
    """The half of `_abandon` that was right, and stays right.

    A request that provably never left must not spend one of the three chances
    to debit a card. The fix is about the schedule and nothing else, so this is
    the control that says the schedule fix did not quietly convert a not-sent
    failure into a spent attempt.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()

    await _sweep(
        db_session,
        tenant,
        subscription,
        invoice,
        transport=_transport(calls, intention_status=400),
        now=NOW,
    )

    assert invoice.collection_attempts == 0
    payment = (
        (await db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id)))
        .scalars()
        .one()
    )
    assert payment.status is PaymentStatus.FAILED
    assert payment.collection_state is CollectionState.ABANDONED


async def test_the_backoff_widens_with_each_abandonment(db_session: AsyncSession) -> None:
    """A cause that is not going away is asked less and less often.

    The delay is derived from how many attempts this invoice has already
    abandoned, so a first refusal is retried soon - the commonest one is
    transient - and a tenth is retried once a day. There is no second retry
    engine: this is the same widening idea as `RETRY_BACKOFF`, keyed on the
    other kind of failure.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=400)
    delays = []

    moment = NOW
    for _ in range(len(ABANDON_BACKOFF) + 2):
        await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=moment)
        assert invoice.next_collection_at is not None
        delays.append(invoice.next_collection_at - moment)
        moment = invoice.next_collection_at

    assert delays[: len(ABANDON_BACKOFF)] == list(ABANDON_BACKOFF)
    assert delays[-1] == ABANDON_BACKOFF[-1], "the widening is capped, not unbounded"
    assert delays == sorted(delays), "each wait is at least as long as the one before"
    assert await _payment_rows(db_session, invoice) == len(delays)


async def test_a_days_worth_of_polls_produces_a_handful_of_rows(
    db_session: AsyncSession,
) -> None:
    """The property in the units an operator cares about.

    A five-minute poll over one simulated day is 288 chances to write a row.
    Before the fix that was 288 rows and 288 log lines; the bound now comes
    from the backoff table rather than from the poll interval, which is the
    whole point - the sweep can be made more frequent without making this
    worse.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=400)

    for poll in range(288):
        await _sweep(
            db_session,
            tenant,
            subscription,
            invoice,
            transport=transport,
            now=NOW + POLL * poll,
        )

    rows = await _payment_rows(db_session, invoice)
    assert rows <= len(ABANDON_BACKOFF) + 2, rows
    assert calls.pays == 0


async def test_an_invoice_recovers_once_the_cause_is_fixed(
    db_session: AsyncSession,
) -> None:
    """The half of F-2 the audit did not describe, and the more damaging half.

    Handing the attempt budget back means the next attempt is number one
    again - and attempt one's idempotency key is already held by the abandoned
    payment row, for ever. So every later poll claimed nothing, reported "not
    due", and the invoice was never debited again *even after the provider was
    fixed*. One transient misconfiguration silently ended automatic collection
    for that invoice.

    Measured against the pre-fix code, thirty days later with a working
    provider: `charged=False reason=not_due`. The retry suffix on the key is
    what makes this test the opposite.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()

    await _sweep(
        db_session,
        tenant,
        subscription,
        invoice,
        transport=_transport(calls, intention_status=400),
        now=NOW,
    )
    assert invoice.collection_attempts == 0

    working = Calls()
    outcome = await _service(db_session, tenant, transport=_transport(working)).collect(
        invoice, subscription=subscription, now=NOW + timedelta(days=30)
    )

    assert outcome.charged, "a fixed provider must be able to collect again"
    assert working.pays == 1
    assert await _payment_rows(db_session, invoice) == 2


async def test_the_ordinary_attempt_key_is_unchanged(db_session: AsyncSession) -> None:
    """The retry suffix appears only where a retry happened.

    An invoice that has never abandoned an attempt claims under exactly the key
    it always did, so the concurrency property `UNIQUE(tenant_id,
    idempotency_key)` carries - two workers claiming attempt three - is the
    same property, not a new one that happens to look like it.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()

    outcome = await _sweep(
        db_session, tenant, subscription, invoice, transport=_transport(calls), now=NOW
    )

    assert outcome is None, "the charge request went out"
    payment = (
        (await db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id)))
        .scalars()
        .one()
    )
    assert payment.idempotency_key == f"auto:{invoice.id}:1"


# --------------------------------------------------- the other two causes


async def test_a_provider_that_cannot_charge_after_all_is_also_backed_off(
    db_session: AsyncSession,
) -> None:
    """`RecurringUnavailableError` is a configuration fact, not a moment.

    A merchant whose Moto integration is missing will still be missing on the
    next poll, so this is the cause most likely to loop - and it reaches
    `_abandon` by a different branch, which is why it has its own test rather
    than sharing the one above.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    provider = _NoMotoProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret="a-test-hmac-secret",
        integration_ids=[4097558],
        moto_integration_id=None,
        transport=_transport(calls),
    )

    first = await _sweep(
        db_session,
        tenant,
        subscription,
        invoice,
        transport=_transport(calls),
        now=NOW,
        provider=provider,
    )
    second = await _sweep(
        db_session,
        tenant,
        subscription,
        invoice,
        transport=_transport(calls),
        now=NOW + POLL,
        provider=provider,
    )

    assert (first, second) == (NOT_SUPPORTED, NOT_DUE)
    assert calls.total == 0, "the capability check runs before any request leaves"
    assert await _payment_rows(db_session, invoice) == 1
    assert invoice.next_collection_at == NOW + ABANDON_BACKOFF[0]


async def test_a_retryable_not_sent_failure_is_backed_off_too(
    db_session: AsyncSession,
) -> None:
    """A 5xx on the intention: transient, so retried - but not instantly.

    The distinction between this and the 4xx above is carried by
    `ChargeNotSentError.retryable` and shows up only in the reason word. The
    schedule is the same either way, because a transient cause retried every
    five minutes is still a loop.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=503)

    reason = await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=NOW)

    assert reason == NOT_SENT
    assert calls.pays == 0
    assert invoice.next_collection_at == NOW + ABANDON_BACKOFF[0]


# ------------------------------------------------------- what must not change


async def test_a_real_decline_still_spends_an_attempt_and_still_exhausts(
    db_session: AsyncSession,
) -> None:
    """The control that separates the two kinds of failure.

    A card that was actually asked and said no has spent a chance, and three of
    those still stop the platform. If the backoff fix had been applied to the
    decline path instead, this test is what would have caught it - a declined
    invoice must reach `ATTEMPTS_EXHAUSTED` and stay there.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()

    def declining(request: httpx.Request) -> httpx.Response:
        if "intention" in str(request.url):
            calls.intentions += 1
            return httpx.Response(
                201,
                json={
                    "id": "pi_auto_1",
                    "client_secret": "csk_auto_1",
                    "payment_keys": [{"key": "a-payment-token", "integration": MOTO_INTEGRATION}],
                },
            )
        calls.pays += 1
        return httpx.Response(400, json={"detail": "declined"})

    transport = httpx.MockTransport(declining)
    moment = NOW
    reasons = []
    for _ in range(MAX_COLLECTION_ATTEMPTS + 2):
        reasons.append(
            await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=moment)
        )
        moment = moment + timedelta(days=4)

    assert calls.pays == MAX_COLLECTION_ATTEMPTS, "the card was really asked, three times"
    assert invoice.collection_attempts == MAX_COLLECTION_ATTEMPTS
    assert reasons[-1] == ATTEMPTS_EXHAUSTED
    assert invoice.next_collection_at is None


async def test_a_settled_invoice_stops_being_collected(db_session: AsyncSession) -> None:
    """A backoff on the row must not outlive the reason for it.

    The schedule written by an abandonment lives on the invoice, so the check
    that it does not keep a *paid* invoice in the sweep is worth making
    explicitly rather than assuming from `_refusal`'s ordering.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=400)
    await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=NOW)

    invoice.status = InvoiceStatus.PAID
    invoice.amount_paid = invoice.amount_due
    await db_session.flush()

    reason = await _sweep(
        db_session,
        tenant,
        subscription,
        invoice,
        transport=transport,
        now=NOW + ABANDON_BACKOFF[0] + POLL,
    )

    assert reason == "not_collectible"
    assert await _payment_rows(db_session, invoice) == 1


async def test_one_workspaces_backoff_does_not_delay_another(
    db_session: AsyncSession,
) -> None:
    """The schedule is a column on an invoice, not a process-wide state."""
    tenant, subscription, invoice = await _workspace(db_session)
    other_tenant, other_subscription, other_invoice = await _workspace(db_session, slug="other")
    calls = Calls()
    transport = _transport(calls, intention_status=400)

    await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=NOW)
    reason = await _sweep(
        db_session,
        other_tenant,
        other_subscription,
        other_invoice,
        transport=transport,
        now=NOW,
    )

    assert reason == PROVIDER_REFUSED
    assert await _payment_rows(db_session, other_invoice) == 1


async def test_the_abandonment_is_logged_with_its_running_count(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator has to be able to see that this is persistent, not a blip.

    One abandoned attempt is noise; the fourth in a row on the same invoice is
    a configuration problem somebody has to fix. The count is on the log entry
    because that is the only thing that distinguishes them, and the next poll
    is there so the entry says when it will be tried again rather than leaving
    the reader to reconstruct it from a backoff table.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    calls = Calls()
    transport = _transport(calls, intention_status=400)

    # Both sweeps inside the capture. Only the second one is what the test is
    # about, but a level raised by whatever ran before this decides whether an
    # INFO record outside the block is kept at all - and a test whose evidence
    # depends on its neighbours is not evidence.
    with caplog.at_level("INFO"):
        await _sweep(db_session, tenant, subscription, invoice, transport=transport, now=NOW)
        await _sweep(
            db_session,
            tenant,
            subscription,
            invoice,
            transport=transport,
            now=NOW + ABANDON_BACKOFF[0],
        )

    # Filtered to this workspace: `caplog` collects from the whole session, so
    # a neighbouring test's abandonment would otherwise be counted here.
    entries = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "billing.collection_attempt_abandoned"
        and getattr(record, "tenant_id", None) == str(tenant.id)
    ]
    # `vars()` rather than attribute access: the fields are set through the
    # logger's `extra=`, so mypy has no way to know they exist on `LogRecord`.
    fields = [vars(entry) for entry in entries]
    assert [field["abandoned_attempts"] for field in fields] == [1, 2]
    assert fields[-1]["next_collection_at"] is not None
    assert fields[-1]["reason"] == PROVIDER_REFUSED
