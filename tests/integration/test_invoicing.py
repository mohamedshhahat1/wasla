"""Invoicing against real rows.

An invoice is a record of a past period, so the claims worth testing are all
about *not changing*: the same period billed twice produces one invoice, a plan
repriced afterwards does not alter what was issued, and a voided invoice is
withdrawn rather than edited.

The rest is money arithmetic, which is exactly where a wrong answer is least
forgivable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, TenantIsolationError, ValidationError
from app.db.models.billing import BillingInterval, Plan, Subscription, SubscriptionStatus
from app.db.models.invoice import Invoice, InvoiceStatus, PaymentStatus
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.integrations.billing import MANUAL_PROVIDER, ManualProvider
from app.integrations.billing.base import ChargeOutcome, PaymentProvider
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.invoice_repository import PlatformInvoiceRepository
from app.services.invoice_service import InvoiceService
from app.services.usage_service import UsageRecorder

pytestmark = pytest.mark.integration

PERIOD_START = datetime(2026, 7, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


class RefusingProvider:
    """Declines every charge, the way a dead card does."""

    @property
    def name(self) -> str:
        return "refusing"

    async def charge(
        self, *, amount: Decimal, currency: str, idempotency_key: str, description: str
    ) -> ChargeOutcome:
        return ChargeOutcome(
            status=PaymentStatus.FAILED,
            amount=amount,
            reference=idempotency_key,
            failure_reason="The card was declined.",
        )


class SucceedingProvider:
    """Collects, and counts how many times it was asked to."""

    def __init__(self) -> None:
        self.charges = 0

    @property
    def name(self) -> str:
        return "succeeding"

    async def charge(
        self, *, amount: Decimal, currency: str, idempotency_key: str, description: str
    ) -> ChargeOutcome:
        self.charges += 1
        return ChargeOutcome(
            status=PaymentStatus.SUCCEEDED,
            amount=amount,
            reference=idempotency_key,
        )


async def _tenant(session: AsyncSession, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(session: AsyncSession, *, code: str = "pro", price: str = "99.00") -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal(price),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscription(session: AsyncSession, tenant: Tenant, plan: Plan) -> Subscription:
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=PERIOD_START,
        current_period_end=PERIOD_END,
    )
    await session.flush()
    return subscription


def _service(
    session: AsyncSession, tenant: Tenant, provider: PaymentProvider | None = None
) -> InvoiceService:
    return InvoiceService(session, tenant_id=tenant.id, provider=provider)


async def _issue(
    session: AsyncSession,
    tenant: Tenant,
    plan: Plan,
    provider: PaymentProvider | None = None,
) -> tuple[Invoice, bool]:
    subscription = await _subscription(session, tenant, plan)
    return await _service(session, tenant, provider).issue_for_period(
        subscription=subscription,
        plan=plan,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        now=NOW,
    )


# ------------------------------------------------------------------ issuing


async def test_an_invoice_records_the_plan_and_what_was_used(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        quantity=120,
        occurred_at=PERIOD_START + timedelta(days=3),
    )
    await db_session.flush()

    invoice, created = await _issue(db_session, tenant, plan)

    assert created is True
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_due == Decimal("99.00")
    assert invoice.plan_code == "pro"
    lines = {line["description"]: line for line in invoice.lines}
    assert lines["Pro plan"]["amount"] == "99.00"
    assert lines["whatsapp_message_sent"]["quantity"] == 120
    # No per-unit price is stored anywhere, so no amount is invented for usage.
    assert lines["whatsapp_message_sent"]["amount"] == "0.00"


async def test_usage_outside_the_period_is_not_billed(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(
        UsageEventType.AI_REQUEST,
        quantity=500,
        occurred_at=PERIOD_START - timedelta(days=1),
    )
    await db_session.flush()

    invoice, _ = await _issue(db_session, tenant, plan)

    descriptions = {line["description"] for line in invoice.lines}
    assert "ai_request" not in descriptions


async def test_billing_the_same_period_twice_issues_one_invoice(db_session: AsyncSession) -> None:
    """A sweep that runs twice, or two replicas at once, must not bill March
    twice. The constraint is the guarantee; this is the no-op that keeps it
    from becoming an integrity error."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(db_session, tenant, plan)
    service = _service(db_session, tenant)

    first, created_first = await service.issue_for_period(
        subscription=subscription,
        plan=plan,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        now=NOW,
    )
    second, created_second = await service.issue_for_period(
        subscription=subscription,
        plan=plan,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        now=NOW,
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_a_free_plan_produces_a_settled_invoice(db_session: AsyncSession) -> None:
    """Still issued: "you were on Starter and used this much" is worth a record
    even when the amount is zero."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, code="starter", price="0.00")

    invoice, _ = await _issue(db_session, tenant, plan)

    assert invoice.status is InvoiceStatus.PAID
    assert invoice.paid_at == NOW
    assert invoice.outstanding == Decimal("0.00")


async def test_a_repriced_plan_does_not_change_an_issued_invoice(db_session: AsyncSession) -> None:
    """The whole reason the amounts are copied rather than joined for."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)

    plan.price = Decimal("199.00")
    plan.name = "Pro (repriced)"
    await db_session.flush()

    assert invoice.amount_due == Decimal("99.00")
    assert invoice.lines[0]["description"] == "Pro plan"


# ------------------------------------------------------------------ payment


async def test_a_declined_charge_is_recorded_and_leaves_the_invoice_open(
    db_session: AsyncSession,
) -> None:
    """A decline is an answer, not an exception: the invoice can be tried again
    and the customer gets a message they can act on."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan, provider=RefusingProvider())

    payment = await _service(db_session, tenant, RefusingProvider()).collect(invoice, now=NOW)

    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == "The card was declined."
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.outstanding == Decimal("99.00")


async def test_a_successful_charge_settles_the_invoice(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    provider = SucceedingProvider()
    invoice, _ = await _issue(db_session, tenant, plan, provider=provider)

    payment = await _service(db_session, tenant, provider).collect(invoice, now=NOW)

    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.paid_at == NOW
    assert invoice.outstanding == Decimal("0.00")


async def test_collecting_twice_does_not_charge_twice(db_session: AsyncSession) -> None:
    """The provider recognises its own idempotency key, and the second attempt
    resolves to the payment already recorded rather than a second charge.

    Driven with the manual provider because it leaves the invoice open: a
    *settled* invoice is refused outright before idempotency is even reached,
    which is the stronger guarantee and is asserted separately below.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan, provider=ManualProvider())
    service = _service(db_session, tenant, ManualProvider())

    first = await service.collect(invoice, now=NOW)
    second = await service.collect(invoice, now=NOW)

    assert first.id == second.id
    assert len(await service.payments_for(invoice.id)) == 1


async def test_a_settled_invoice_cannot_be_collected_again(db_session: AsyncSession) -> None:
    """Refused before the provider is called at all: the surest way not to
    charge a customer twice is not to ask."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    provider = SucceedingProvider()
    invoice, _ = await _issue(db_session, tenant, plan, provider=provider)
    service = _service(db_session, tenant, provider)
    await service.collect(invoice, now=NOW)

    with pytest.raises(ConflictError):
        await service.collect(invoice, now=NOW)

    assert provider.charges == 1


async def test_the_manual_provider_never_claims_to_have_collected(db_session: AsyncSession) -> None:
    """A provider that pretended a bank transfer had arrived would put a paid
    invoice in front of a finance team that has not paid."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan, provider=ManualProvider())

    payment = await _service(db_session, tenant, ManualProvider()).collect(invoice, now=NOW)

    assert payment.status is PaymentStatus.PENDING
    assert payment.provider == MANUAL_PROVIDER
    assert invoice.status is InvoiceStatus.OPEN


async def test_money_that_arrived_outside_the_system_can_be_recorded(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)

    payment = await _service(db_session, tenant).record_payment(
        invoice_id=invoice.id,
        amount=Decimal("99.00"),
        provider=MANUAL_PROVIDER,
        reference="bank-transfer-8891",
        now=NOW,
    )

    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID


async def test_a_part_payment_leaves_the_invoice_open(db_session: AsyncSession) -> None:
    """A customer who paid half has paid half."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)
    service = _service(db_session, tenant)

    await service.record_payment(
        invoice_id=invoice.id,
        amount=Decimal("40.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )

    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.outstanding == Decimal("59.00")

    await service.record_payment(
        invoice_id=invoice.id,
        amount=Decimal("59.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )
    status: InvoiceStatus = invoice.status
    assert status is InvoiceStatus.PAID


async def test_an_overpayment_leaves_nothing_outstanding_rather_than_a_negative(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)

    await _service(db_session, tenant).record_payment(
        invoice_id=invoice.id,
        amount=Decimal("150.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )

    assert invoice.outstanding == Decimal("0.00")


async def test_a_payment_for_nothing_is_refused(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).record_payment(
            invoice_id=invoice.id,
            amount=Decimal("0.00"),
            provider=MANUAL_PROVIDER,
        )


async def test_every_attempt_is_kept(db_session: AsyncSession) -> None:
    """A failed attempt is not forgotten when a later one succeeds: the history
    is what a dispute turns on."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan, provider=RefusingProvider())
    await _service(db_session, tenant, RefusingProvider()).collect(invoice, now=NOW)
    await _service(db_session, tenant).record_payment(
        invoice_id=invoice.id,
        amount=Decimal("99.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )

    payments = await _service(db_session, tenant).payments_for(invoice.id)
    # Compared as a set, not a sequence. Both rows are written in one
    # transaction, and PostgreSQL's `now()` is constant across it - so they
    # share `created_at` and their relative order is decided by a random uuid.
    # In production the attempts are separated in time; here the claim being
    # made is that the failure survived the success, not what order they read
    # in.
    assert {payment.status for payment in payments} == {
        PaymentStatus.FAILED,
        PaymentStatus.SUCCEEDED,
    }


# -------------------------------------------------------------------- voiding


async def test_a_mistaken_invoice_is_withdrawn_rather_than_edited(db_session: AsyncSession) -> None:
    """The customer has seen it. A bill that silently changes is worse than one
    that is visibly withdrawn."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)

    voided = await _service(db_session, tenant).void(invoice.id, reason="Issued in error.")

    assert voided.status is InvoiceStatus.VOID
    assert voided.notes == "Issued in error."
    assert voided.amount_due == Decimal("99.00")


async def test_a_paid_invoice_cannot_be_voided(db_session: AsyncSession) -> None:
    """That is a refund, which is a different operation and a different
    conversation."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)
    await _service(db_session, tenant).record_payment(
        invoice_id=invoice.id,
        amount=Decimal("99.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).void(invoice.id)


async def test_a_voided_invoice_cannot_be_paid(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, tenant, plan)
    await _service(db_session, tenant).void(invoice.id)

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).record_payment(
            invoice_id=invoice.id,
            amount=Decimal("99.00"),
            provider=MANUAL_PROVIDER,
        )


# ------------------------------------------------------------------ isolation


async def test_one_workspace_cannot_read_anothers_invoice(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session)
    invoice, _ = await _issue(db_session, acme, plan)

    with pytest.raises(TenantIsolationError):
        await _service(db_session, rival).get(invoice.id)
    assert await _service(db_session, rival).list_invoices() == []


async def test_an_invoice_that_does_not_exist_is_not_found(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    with pytest.raises(TenantIsolationError):
        await _service(db_session, tenant).get(uuid.uuid4())


# ------------------------------------------------------------------- revenue


async def test_platform_revenue_counts_only_what_was_paid(db_session: AsyncSession) -> None:
    """An issued invoice is a hope, not revenue."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session)

    paid, _ = await _issue(db_session, acme, plan)
    await _service(db_session, acme).record_payment(
        invoice_id=paid.id,
        amount=Decimal("99.00"),
        provider=MANUAL_PROVIDER,
        now=NOW,
    )
    await _issue(db_session, rival, plan)
    await db_session.flush()

    revenue = await PlatformInvoiceRepository(db_session).revenue()
    outstanding = await PlatformInvoiceRepository(db_session).outstanding()

    assert [(row.currency, row.amount) for row in revenue] == [("USD", Decimal("99.00"))]
    assert [(row.currency, row.amount) for row in outstanding] == [("USD", Decimal("99.00"))]


async def test_revenue_is_grouped_by_currency(db_session: AsyncSession) -> None:
    """Adding dollars to euros produces a number that is wrong in a way nobody
    can see."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    dollars = await _plan(db_session, code="usd-plan")
    euros = await _plan(db_session, code="eur-plan")
    euros.currency = "EUR"
    await db_session.flush()

    first, _ = await _issue(db_session, acme, dollars)
    second, _ = await _issue(db_session, rival, euros)
    for tenant, invoice in ((acme, first), (rival, second)):
        await _service(db_session, tenant).record_payment(
            invoice_id=invoice.id,
            amount=Decimal("99.00"),
            provider=MANUAL_PROVIDER,
            now=NOW,
        )
    await db_session.flush()

    revenue = await PlatformInvoiceRepository(db_session).revenue()
    assert {row.currency for row in revenue} == {"USD", "EUR"}
