"""The database keeps the books on its own (DB-001, DB-017; migration 0074).

`test_settlement_concurrency.py` proves the application settles one invoice
once under concurrency. This file proves what stands beneath it when a writer
forgets the lock - an operator's repair script, a future code path - by
writing the ledger with plain SQL and asking PostgreSQL to check it.

The checks are deferred to commit, because a settlement writes the invoice
and the payment in whichever order its flushes happen. These tests run inside
the suite's rolled-back transaction, so they fire the queued checks explicitly
with `SET CONSTRAINTS ALL IMMEDIATE` - the same checks a commit would run.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.db.errors import sqlstate
from app.db.models.billing import BillingInterval, Plan, Subscription, SubscriptionStatus
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.checkout import CallbackEvent
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.services.checkout_service import APPLIED, CheckoutService
from app.services.invoice_service import InvoiceService
from app.services.plan_catalog import PlanCatalog
from tests.billing_fixtures import price_terms
from tests.paymob_orders import CARD_INTEGRATION_ID, order_for, order_from_request

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
PRICE = Decimal("99.00")
HMAC_SECRET = "backstop-hmac-synthetic"


async def _checked(session: AsyncSession) -> None:
    """Run the checks a commit would run, now."""
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


async def _refused_at_commit(
    session: AsyncSession, statement: str, params: dict[str, object]
) -> str:
    """Execute `statement` and prove the commit-time checks refuse it."""
    with pytest.raises((IntegrityError, DBAPIError)) as refused:
        async with session.begin_nested():
            await session.execute(text(statement), params)
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    return sqlstate(refused.value) or ""


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Backstop", slug=f"backstop-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _open_invoice(session: AsyncSession, tenant: Tenant) -> Invoice:
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.CHECKOUT,
        plan_code="pro",
        amount_due=PRICE,
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=NOW,
        period_end=NOW + timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def _paid_invoice(session: AsyncSession, tenant: Tenant) -> tuple[Invoice, Payment]:
    """An invoice paid by one applied payment, as settlement leaves it."""
    invoice = await _open_invoice(session, tenant)
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.SUCCEEDED,
        amount=PRICE,
        currency="EGP",
        provider="paymob",
        provider_reference=str(uuid.uuid4().int % 10**12),
        refunded_amount=Decimal("0.00"),
        processed_at=NOW,
        applied_at=NOW,
    )
    session.add(payment)
    invoice.amount_paid = PRICE
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = NOW
    await session.flush()
    await _checked(session)
    return invoice, payment


_SECOND_PAYMENT = (
    "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency, provider,"
    " provider_reference, refunded_amount, is_automatic, processed_at, applied_at, revision)"
    " VALUES (:id, :tenant, :invoice, 'succeeded', 99.00, 'EGP', 'paymob', :reference, 0,"
    " false, :now, :applied, 1)"
)


async def test_a_second_applied_payment_on_a_paid_invoice_is_refused(
    db_session: AsyncSession,
) -> None:
    """The audit's Y50 and DB-F03: two payments counted against one invoice."""
    tenant = await _tenant(db_session)
    invoice, _ = await _paid_invoice(db_session, tenant)
    state = await _refused_at_commit(
        db_session,
        _SECOND_PAYMENT,
        {
            "id": uuid.uuid4(),
            "tenant": tenant.id,
            "invoice": invoice.id,
            "reference": "second-transaction",
            "now": NOW,
            "applied": NOW,
        },
    )
    assert state == "23000"


async def test_collected_money_is_either_applied_or_held_with_an_incident(
    db_session: AsyncSession,
) -> None:
    """A duplicate payment may be kept - only if an incident says why."""
    tenant = await _tenant(db_session)
    invoice, _ = await _paid_invoice(db_session, tenant)
    unexplained = uuid.uuid4()
    params = {
        "id": unexplained,
        "tenant": tenant.id,
        "invoice": invoice.id,
        "reference": "held-transaction",
        "now": NOW,
        "applied": None,
    }
    assert await _refused_at_commit(db_session, _SECOND_PAYMENT, params) == "23000"

    # The same row with the incident a refused settlement raises: accepted.
    await db_session.execute(text(_SECOND_PAYMENT), params)
    db_session.add(
        BillingIncident(
            tenant_id=tenant.id,
            kind=BillingIncidentKind.DUPLICATE_PAYMENT,
            status="open",
            dedupe_key=f"duplicate_payment:{unexplained}:held-transaction",
            payment_id=unexplained,
            invoice_id=invoice.id,
            amount=PRICE,
            currency="EGP",
        )
    )
    await db_session.flush()
    await _checked(db_session)


async def test_amount_paid_cannot_be_written_without_the_money(db_session: AsyncSession) -> None:
    """An invoice marked as holding money no applied payment brought."""
    tenant = await _tenant(db_session)
    invoice = await _open_invoice(db_session, tenant)
    await _checked(db_session)
    state = await _refused_at_commit(
        db_session,
        "UPDATE invoices SET amount_paid = 50.00 WHERE id = :id",
        {"id": invoice.id},
    )
    assert state == "23000"


async def test_only_collected_money_can_be_applied(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    invoice = await _open_invoice(db_session, tenant)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency,"
                    " provider, refunded_amount, is_automatic, applied_at, revision) VALUES"
                    " (:id, :tenant, :invoice, 'pending', 99.00, 'EGP', 'paymob', 0, false,"
                    " :now, 1)"
                ),
                {"id": uuid.uuid4(), "tenant": tenant.id, "invoice": invoice.id, "now": NOW},
            )


# ------------------------------------------------------ manual references


async def test_manual_references_repeat_across_workspaces_not_within_one(
    db_session: AsyncSession,
) -> None:
    """DB-017: two workspaces may both write "BT-1"; one workspace may not twice.

    Before 0074 the operator's text was the payment's `provider_reference`,
    unique across the platform, and the second workspace's transfer failed
    with an unhandled IntegrityError - a 500.
    """
    first, second = await _tenant(db_session), await _tenant(db_session)
    for tenant in (first, second):
        invoice = await _open_invoice(db_session, tenant)
        payment = await InvoiceService(db_session, tenant_id=tenant.id).record_payment(
            invoice_id=invoice.id,
            amount=Decimal("40.00"),
            provider="bank_transfer",
            reference="BT-1",
            now=NOW,
        )
        assert payment.manual_reference == "BT-1"
        assert payment.provider_reference == f"manual:{payment.id}"

    invoice = await _open_invoice(db_session, first)
    with pytest.raises(ConflictError):
        await InvoiceService(db_session, tenant_id=first.id).record_payment(
            invoice_id=invoice.id,
            amount=Decimal("40.00"),
            provider="bank_transfer",
            reference="BT-1",
            now=NOW,
        )
    await _checked(db_session)


async def test_a_processor_transaction_recorded_by_hand_stays_globally_unique(
    db_session: AsyncSession,
) -> None:
    """A Paymob transaction id is a fact, not free text: one ledger row, ever."""
    first, second = await _tenant(db_session), await _tenant(db_session)
    invoice = await _open_invoice(db_session, first)
    payment = await InvoiceService(db_session, tenant_id=first.id).record_payment(
        invoice_id=invoice.id, amount=PRICE, provider="paymob", reference="541130792", now=NOW
    )
    assert payment.provider_reference == "541130792"
    assert payment.manual_reference is None

    other = await _open_invoice(db_session, second)
    with pytest.raises(ConflictError):
        await InvoiceService(db_session, tenant_id=second.id).record_payment(
            invoice_id=other.id, amount=PRICE, provider="paymob", reference="541130792", now=NOW
        )


# ------------------------------------------------------ held money refunded


def _provider() -> PaymobProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": f"pi_test_{uuid.uuid4().hex[:8]}",
                "client_secret": f"csk_test_{uuid.uuid4().hex[:8]}",
                "intention_order_id": order_from_request(request),
            },
        )

    return PaymobProvider(
        secret_key="sk_test_backstop",
        public_key="pk_test_backstop",
        hmac_secret=HMAC_SECRET,
        integration_ids=[CARD_INTEGRATION_ID],
        transport=httpx.MockTransport(handler),
    )


def _event(
    provider: PaymobProvider, payment: Payment, *, transaction: int, refunded: int | None = None
) -> CallbackEvent:
    obj = {
        "id": transaction,
        "pending": False,
        "amount_cents": 9900,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": refunded is not None,
        "is_3d_secure": True,
        "integration_id": CARD_INTEGRATION_ID,
        "has_parent_transaction": refunded is not None,
        "order": {"id": int(str(payment.provider_order_id)), "merchant_order_id": str(payment.id)},
        "is_live": False,
        "created_at": "2026-09-26T12:00:00.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    if refunded is not None:
        obj["refunded_amount_cents"] = refunded
        obj["parent_transaction"] = int(payment.provider_reference or 0)
    return provider.verify_callback(
        payload=json.dumps({"type": "TRANSACTION", "obj": obj}).encode(),
        signature=hmac_signature(obj, secret=HMAC_SECRET),
    )


async def test_refunding_held_money_leaves_the_paid_invoice_alone(
    db_session: AsyncSession,
) -> None:
    """The operator's answer to a duplicate: give it back, and nothing else.

    Before, a refund confirmed for the *held* second payment subtracted from
    the invoice the first payment paid - reopening a paid bill and withdrawing
    the plan the customer had paid for once.
    """
    now = NOW
    tenant = await _tenant(db_session)
    plan = Plan(
        code=f"bk-{uuid.uuid4().hex[:8]}",
        name="Pro",
        price=PRICE,
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={},
    )
    db_session.add(plan)
    await db_session.flush()
    version = await PlanCatalog(db_session).current_version(plan)
    assert version is not None
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        plan_version_id=version.id,
        plan_price_id=(await price_terms(db_session, version.id)).get("plan_price_id"),
        status=SubscriptionStatus.PAST_DUE,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        billing_anchor_at=now,
        cancel_at_period_end=False,
    )
    db_session.add(subscription)
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.RENEWAL,
        plan_code=plan.code,
        plan_version_id=version.id,
        **(await price_terms(db_session, version.id)),
        amount_due=PRICE,
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=now,
        period_end=now + timedelta(days=30),
        issued_at=now,
        lines=[],
    )
    db_session.add(invoice)
    await db_session.flush()
    invoice.subscription_id = subscription.id
    pages = []
    for _ in range(2):
        page = Payment(
            tenant_id=tenant.id,
            invoice_id=invoice.id,
            status=PaymentStatus.PENDING,
            amount=PRICE,
            currency="EGP",
            provider="paymob",
            refunded_amount=Decimal("0.00"),
            is_automatic=False,
        )
        db_session.add(page)
        await db_session.flush()
        page.provider_order_id = str(order_for(page.id))
        pages.append(page)
    await db_session.flush()

    provider = _provider()
    service = CheckoutService(db_session, tenant_id=tenant.id, provider=provider)
    assert await service.apply(_event(provider, pages[0], transaction=811), now=now) == APPLIED
    assert await service.apply(_event(provider, pages[1], transaction=812), now=now) != APPLIED
    await db_session.refresh(pages[1])
    assert pages[1].applied_at is None

    refund = _event(provider, pages[1], transaction=813, refunded=9900)
    assert await service.apply(refund, now=now) == APPLIED
    await db_session.refresh(invoice)
    await db_session.refresh(pages[1])
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == PRICE
    assert pages[1].status is PaymentStatus.REFUNDED
    await _checked(db_session)
