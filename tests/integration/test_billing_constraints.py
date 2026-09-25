"""The database refuses bad money on its own (BILL-12, BILL-19, BILL-20).

Direct SQL, not service calls: the audit's probe P20 showed the database
accepting a negative price, a negative trial, currency `ZZZ` and negative
invoice and payment amounts, with only the service layer - and for plans, no
service at all - in front of them. These are the defence in depth beneath every
path, proved against real PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import BillingInterval, Plan, PlanVersion
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.db.models.tenant import Tenant
from app.services.plan_catalog import PlanCatalog

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 1, tzinfo=UTC)


async def _refused(session: AsyncSession, statement: str, params: dict[str, object]) -> None:
    with pytest.raises((IntegrityError, DBAPIError)):
        async with session.begin_nested():
            await session.execute(text(statement), params)


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Ledger", slug=f"ledger-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _invoice(session: AsyncSession, tenant: Tenant) -> Invoice:
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.CHECKOUT,
        plan_code="pro",
        amount_due=Decimal("99.00"),
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=NOW,
        period_end=NOW + timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    return invoice


@pytest.mark.parametrize(
    ("column", "value"),
    [("price", "-5.00"), ("trial_days", -3), ("currency", "ZZZ")],
)
async def test_the_database_refuses_an_invalid_plan(
    db_session: AsyncSession, column: str, value: object
) -> None:
    values = {"price": "10.00", "trial_days": 0, "currency": "EGP", column: value}
    await _refused(
        db_session,
        "INSERT INTO plans (id, code, name, price, currency, interval, trial_days, limits,"
        " is_public, is_active, sort_order) VALUES (:id, :code, 'Bad', :price, :currency,"
        " 'monthly', :trial_days, '{}', true, true, 0)",
        {"id": uuid.uuid4(), "code": f"bad-{uuid.uuid4().hex[:8]}", **values},
    )


@pytest.mark.parametrize(
    ("amount_due", "amount_paid", "currency"),
    [
        ("-1.00", "0.00", "EGP"),
        ("10.00", "-1.00", "EGP"),
        ("10.00", "11.00", "EGP"),
        ("10.00", "0.00", "USD"),
    ],
)
async def test_the_database_refuses_an_invalid_invoice(
    db_session: AsyncSession, amount_due: str, amount_paid: str, currency: str
) -> None:
    tenant = await _tenant(db_session)
    await _refused(
        db_session,
        "INSERT INTO invoices (id, tenant_id, status, purpose, plan_code, amount_due,"
        " amount_paid, currency, period_start, period_end, lines, collection_attempts)"
        " VALUES (:id, :tenant, 'open', 'checkout', 'pro', :due, :paid, :currency, :start,"
        " :end, '[]', 0)",
        {
            "id": uuid.uuid4(),
            "tenant": tenant.id,
            "due": amount_due,
            "paid": amount_paid,
            "currency": currency,
            "start": NOW,
            "end": NOW + timedelta(days=30),
        },
    )


@pytest.mark.parametrize(
    ("amount", "refunded"),
    [("0.00", "0.00"), ("-5.00", "0.00"), ("10.00", "-1.00"), ("10.00", "10.01")],
)
async def test_the_database_refuses_an_invalid_payment(
    db_session: AsyncSession, amount: str, refunded: str
) -> None:
    tenant = await _tenant(db_session)
    invoice = await _invoice(db_session, tenant)
    await _refused(
        db_session,
        "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency, provider,"
        " refunded_amount, is_automatic) VALUES (:id, :tenant, :invoice, 'pending', :amount,"
        " 'EGP', 'paymob', :refunded, false)",
        {
            "id": uuid.uuid4(),
            "tenant": tenant.id,
            "invoice": invoice.id,
            "amount": amount,
            "refunded": refunded,
        },
    )


async def test_a_plan_version_cannot_be_rewritten(db_session: AsyncSession) -> None:
    """Immutability is the table's, not the code's: a trigger refuses any UPDATE."""
    plan = Plan(
        code=f"frozen-{uuid.uuid4().hex[:8]}",
        name="Frozen",
        price=Decimal("99.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={"agents": 5},
    )
    db_session.add(plan)
    await db_session.flush()
    version = await PlanCatalog(db_session).current_version(plan)
    assert isinstance(version, PlanVersion)

    await _refused(
        db_session,
        "UPDATE plan_versions SET price = 1 WHERE id = :id",
        {"id": version.id},
    )


async def test_a_tenant_with_a_financial_ledger_cannot_be_deleted(
    db_session: AsyncSession,
) -> None:
    """BILL-19: invoices, payments and their events no longer cascade away."""
    tenant = await _tenant(db_session)
    invoice = await _invoice(db_session, tenant)
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
        refunded_amount=Decimal("0.00"),
    )
    db_session.add(payment)
    await db_session.flush()

    await _refused(db_session, "DELETE FROM tenants WHERE id = :id", {"id": tenant.id})
    await _refused(db_session, "DELETE FROM invoices WHERE id = :id", {"id": invoice.id})

    survivors = await db_session.scalar(
        text("SELECT count(*) FROM payments WHERE id = :id"), {"id": payment.id}
    )
    assert survivors == 1


async def test_an_invoice_alone_keeps_its_tenant_row(db_session: AsyncSession) -> None:
    """BILL-19 (mutation R-BILL-19): the invoice's own foreign key refuses the delete.

    With a payment present, the payment's RESTRICT would refuse it anyway and
    hide a cascading invoice key. An unpaid invoice with nothing under it is
    where only `invoices.tenant_id` stands between a stray DELETE and the bill.
    """
    tenant = await _tenant(db_session)
    invoice = await _invoice(db_session, tenant)

    await _refused(db_session, "DELETE FROM tenants WHERE id = :id", {"id": tenant.id})

    survivors = await db_session.scalar(
        text("SELECT count(*) FROM invoices WHERE id = :id"), {"id": invoice.id}
    )
    assert survivors == 1
