"""Refunds: asking for money to go back, and believing that it did.

Two separate things, deliberately, and most of this file is about keeping them
separate. `RefundService` asks a processor to reverse a payment and records
that it asked. Nothing is marked returned until a signed callback says the
reversal happened - the same path a refund issued from the provider's own
dashboard arrives on, which is why there is exactly one place that writes
`refunded_amount`.

The refusals are the interesting half. A refund moves money *out*, so every
question about whether one is allowed is answered from the database: whose
payment it is, whether it ever collected anything, whether it has already been
given back. The amount is never asked for - it is the payment's own unreturned
balance - so there is no field a caller can send to be refunded more than they
paid.

The provider runs for real against `httpx.MockTransport`: the real refund body
is built, the real response parsed, the real HMAC checked on the callbacks.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import BillingInterval, LimitKey, Plan
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.billing.base import ProviderError
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.services.checkout_service import (
    APPLIED,
    DUPLICATE,
    MISMATCHED,
    NO_CHANGE,
    REFUSED,
    UNMATCHED,
    CheckoutService,
)
from app.services.refund_service import RefundService

pytestmark = pytest.mark.integration

HMAC_SECRET = "a-test-hmac-secret"
PAID_TRANSACTION = "192036465"
REVERSAL_TRANSACTION = "579305"


def _refund_transport(
    seen: list[dict] | None = None,
    *,
    response: httpx.Response | None = None,
    error: Exception | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        if seen is not None:
            seen.append(json.loads(request.content))
        return response or httpx.Response(
            200,
            json={"id": int(REVERSAL_TRANSACTION), "success": True, "pending": False},
        )

    return httpx.MockTransport(handler)


def _provider(transport: httpx.MockTransport | None = None) -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
        transport=transport if transport is not None else _refund_transport(),
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


async def _paid(
    session,
    tenant: Tenant,
    *,
    amount: str = "99.00",
    currency: str = "EGP",
    transaction: str = PAID_TRANSACTION,
    status: PaymentStatus = PaymentStatus.SUCCEEDED,
) -> tuple[Invoice, Payment]:
    """An invoice that has been collected, as a settled checkout leaves it."""
    from datetime import UTC, datetime

    plan = Plan(
        code="pro",
        name="Pro",
        price=Decimal(amount),
        currency=currency,
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.AGENTS.value: 5},
    )
    session.add(plan)
    moment = datetime(2026, 8, 1, tzinfo=UTC)
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.PAID if status is PaymentStatus.SUCCEEDED else InvoiceStatus.OPEN,
        plan_code="pro",
        amount_due=Decimal(amount),
        amount_paid=Decimal(amount) if status is PaymentStatus.SUCCEEDED else Decimal("0.00"),
        currency=currency,
        period_start=moment,
        period_end=moment,
        lines=[],
        paid_at=moment if status is PaymentStatus.SUCCEEDED else None,
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=status,
        amount=Decimal(amount),
        currency=currency,
        provider="paymob",
        provider_reference=transaction if status is PaymentStatus.SUCCEEDED else None,
        refunded_amount=Decimal("0.00"),
        processed_at=moment,
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


def _reversal(
    *,
    reference: str | None,
    transaction: str = PAID_TRANSACTION,
    amount_cents: int = 9900,
    refunded_cents: int | None = None,
    parent: str | None = None,
    currency: str = "EGP",
    voided: bool = False,
) -> dict:
    """A callback shaped like the documented reversal notification."""
    body = {
        "id": int(transaction),
        "pending": False,
        "amount_cents": amount_cents,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": voided,
        "is_refunded": not voided,
        "is_3d_secure": True,
        "integration_id": 4097558,
        "has_parent_transaction": parent is not None,
        "parent_transaction": int(parent) if parent else None,
        "order": {"id": 217503754, "merchant_order_id": reference},
        "created_at": "2026-08-29T11:33:44.592345",
        "currency": currency,
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    if refunded_cents is not None:
        body["refunded_amount_cents"] = refunded_cents
    return body


async def _apply(session, tenant, transaction: dict) -> str:
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    signature = hmac_signature(transaction, secret=HMAC_SECRET)
    event = _provider().verify_callback(payload=body, signature=signature)
    service = CheckoutService(session, tenant_id=tenant.id, provider=_provider())
    return await service.apply(event)


# ------------------------------------------------------------ asking for one


async def test_a_refund_asks_the_provider_for_the_unreturned_balance(db_session):
    """The amount is ours, computed from the row, and never a caller's.

    A request that could name a figure is a request that could name a larger
    one.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    _, payment = await _paid(db_session, tenant, amount="99.00")
    seen: list[dict] = []

    await RefundService(
        db_session,
        tenant_id=tenant.id,
        provider=_provider(_refund_transport(seen)),
    ).refund(payment.id, actor=user)

    assert seen == [{"transaction_id": PAID_TRANSACTION, "amount_cents": 9900}]


async def test_asking_for_a_refund_does_not_make_the_money_come_back(db_session):
    """The whole reason the request and the confirmation are separate.

    A 200 from a refund API means the reversal was accepted, not that a
    customer has their money. Marking the payment refunded here would be
    telling them something that is not true yet - and would make the callback
    that says it *is* true look like a duplicate.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    invoice, payment = await _paid(db_session, tenant)

    await RefundService(db_session, tenant_id=tenant.id, provider=_provider()).refund(
        payment.id,
        actor=user,
    )

    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.refunded_amount == Decimal("0.00")
    assert payment.refunded_at is None
    assert invoice.status is InvoiceStatus.PAID
    # But the asking is recorded, which is what makes an unconfirmed refund
    # findable rather than invisible.
    assert payment.refund_requested_at is not None
    assert payment.refund_reference == REVERSAL_TRANSACTION


async def test_the_request_is_audited_with_who_asked(db_session):
    """A refund is the one billing action that moves money out.

    "Who authorised this" is the question the audit log exists to answer.
    """
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    _, payment = await _paid(db_session, tenant)

    await RefundService(db_session, tenant_id=tenant.id, provider=_provider()).refund(
        payment.id,
        actor=user,
        reason="Duplicate charge",
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == AuditAction.PAYMENT_REFUND_REQUESTED)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_id == user.id
    assert rows[0].meta["amount"] == "99.00"
    assert rows[0].meta["reason"] == "Duplicate charge"


@pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.FAILED])
async def test_a_payment_that_never_collected_cannot_be_refunded(db_session, status):
    """There is no money here to give back.

    Attempting it would ask Paymob to reverse a transaction that does not
    exist, and a provider error is a much worse way to learn this than a
    conflict.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant, status=status)

    with pytest.raises(ConflictError):
        await RefundService(db_session, tenant_id=tenant.id, provider=_provider()).refund(
            payment.id
        )


async def test_a_second_refund_request_is_refused(db_session):
    """Reversing the same money twice is the failure this guards.

    The first request stored the reversal's reference; a second would ask
    Paymob to refund a transaction it is already refunding.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    service = RefundService(db_session, tenant_id=tenant.id, provider=_provider())

    await service.refund(payment.id)

    with pytest.raises(ConflictError):
        await service.refund(payment.id)


async def test_a_payment_taken_by_hand_cannot_be_reversed_by_a_processor(db_session):
    """A bank transfer somebody typed in has no transaction to reverse.

    Refunding it is a person moving money, not an API call, and pretending
    otherwise would send Paymob a null transaction id.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    payment.provider_reference = None
    await db_session.flush()

    with pytest.raises(ConflictError):
        await RefundService(db_session, tenant_id=tenant.id, provider=_provider()).refund(
            payment.id
        )


async def test_a_payment_from_another_provider_is_not_reversed_through_this_one(db_session):
    """Sending one processor another's transaction id refunds nothing.

    Or, on a bad day, refunds a transaction that happens to share the number.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    payment.provider = "manual"
    await db_session.flush()

    with pytest.raises(ConflictError):
        await RefundService(db_session, tenant_id=tenant.id, provider=_provider()).refund(
            payment.id
        )


async def test_another_workspaces_payment_cannot_be_refunded(db_session):
    """The isolation boundary, on the operation that moves money out.

    Not-found rather than forbidden, so a caller cannot learn which payment ids
    are real by reading which refusal comes back.
    """
    acme = await _tenant(db_session, "acme")
    globex = await _tenant(db_session, "globex")
    _, payment = await _paid(db_session, acme)

    with pytest.raises(NotFoundError):
        await RefundService(db_session, tenant_id=globex.id, provider=_provider()).refund(
            payment.id
        )


async def test_a_provider_failure_leaves_the_refund_repeatable(db_session):
    """Asked, not accepted - and therefore safe to ask again.

    The reference is what marks a refund as in flight, and it is written only
    after the provider accepts. A refund that failed on the way out must not
    become a refund nobody can retry.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    transport = _refund_transport(error=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderError) as caught:
        await RefundService(
            db_session,
            tenant_id=tenant.id,
            provider=_provider(transport),
        ).refund(payment.id)

    assert caught.value.retryable
    assert payment.refund_reference is None
    # The attempt is still on the record, so an operator can see it was tried.
    assert payment.refund_requested_at is not None


async def test_a_refund_the_provider_declines_is_not_recorded_as_accepted(db_session):
    """A 200 saying `success: false` is a refusal wearing a success's clothes."""
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    transport = _refund_transport(
        response=httpx.Response(200, json={"id": 1, "success": False}),
    )

    with pytest.raises(ProviderError):
        await RefundService(
            db_session,
            tenant_id=tenant.id,
            provider=_provider(transport),
        ).refund(payment.id)

    assert payment.refund_reference is None


# ---------------------------------------------------- believing it happened


async def test_a_reversal_callback_gives_the_money_back_and_reopens_the_invoice(db_session):
    """`amount_paid` records money we hold, so giving it back uncovers the bill.

    An invoice whose payments no longer cover it is not paid, whatever it said
    a moment ago. Voiding it afterwards - because the customer is leaving - is
    a separate deliberate act.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant)

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=9900),
    )

    assert outcome == APPLIED
    assert payment.status is PaymentStatus.REFUNDED
    assert payment.refunded_amount == Decimal("99.00")
    assert payment.refunded_at is not None
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.paid_at is None


async def test_a_reversal_is_not_swallowed_as_a_duplicate_of_the_payment(db_session):
    """The defect the composite event id exists to prevent.

    Paymob sends the refund notification about the *parent* transaction, so its
    `obj.id` is the id of the payment we already recorded an event for. Keying
    idempotency on the transaction alone would file the refund as a duplicate
    delivery of the payment and give nobody their money back.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant, status=PaymentStatus.PENDING)

    # The original collection, recorded first, exactly as it would have been:
    # an open invoice and a pending attempt, settled by the success callback.
    collected = {
        **_reversal(reference=str(payment.id)),
        "is_refunded": False,
        "is_voided": False,
    }
    assert await _apply(db_session, tenant, collected) == APPLIED
    assert invoice.status is InvoiceStatus.PAID

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=9900),
    )

    assert outcome == APPLIED
    assert payment.status is PaymentStatus.REFUNDED


async def test_a_reversal_is_matched_by_the_transaction_it_reverses(db_session):
    """The documented fallback, for a callback that does not carry our own id.

    Still an identifier this system wrote down itself - the transaction id
    recorded when the money arrived - rather than anything the caller invented.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant)

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(
            reference=None,
            transaction=REVERSAL_TRANSACTION,
            parent=PAID_TRANSACTION,
            amount_cents=9900,
        ),
    )

    assert outcome == APPLIED
    assert payment.refunded_amount == Decimal("99.00")
    assert invoice.status is InvoiceStatus.OPEN


async def test_a_repeated_reversal_callback_changes_nothing(db_session):
    """A provider retries anything it did not get a 2xx for."""
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)
    reversal = _reversal(reference=str(payment.id), refunded_cents=9900)

    assert await _apply(db_session, tenant, reversal) == APPLIED
    assert await _apply(db_session, tenant, reversal) == DUPLICATE

    assert payment.refunded_amount == Decimal("99.00")


async def test_a_partial_reversal_then_the_rest_adds_up_once(db_session):
    """The running total is cumulative, so the *difference* is what came back.

    Adding the reported figure each time would return 150 of a 99 payment.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant)

    first = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), transaction="1001", refunded_cents=4000),
    )
    second = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), transaction="1002", refunded_cents=9900),
    )

    assert (first, second) == (APPLIED, APPLIED)
    assert payment.refunded_amount == Decimal("99.00")
    assert payment.status is PaymentStatus.REFUNDED
    assert invoice.amount_paid == Decimal("0.00")


async def test_a_partial_reversal_leaves_the_payment_partly_collected(db_session):
    """Some of it came back, so the payment is not simply "refunded"."""
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant)

    await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=4000),
    )

    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.refunded_amount == Decimal("40.00")
    assert invoice.amount_paid == Decimal("59.00")
    assert invoice.status is InvoiceStatus.OPEN


async def test_a_reversal_larger_than_the_payment_is_refused(db_session):
    """A provider cannot give back more than it took, so this is not believed.

    Applying it would drive `amount_paid` negative and hand somebody money the
    invoice never held.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant, amount="99.00")

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=500000),
    )

    assert outcome == MISMATCHED
    assert payment.refunded_amount == Decimal("0.00")
    assert invoice.status is InvoiceStatus.PAID


async def test_a_reversal_in_another_currency_is_refused(db_session):
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant, currency="EGP")

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=9900, currency="USD"),
    )

    assert outcome == MISMATCHED
    assert payment.refunded_amount == Decimal("0.00")


async def test_a_reversal_naming_nothing_of_ours_is_recorded_and_ignored(db_session):
    tenant = await _tenant(db_session)
    await _paid(db_session, tenant)

    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=None, transaction="4242", parent="4243"),
    )

    assert outcome == UNMATCHED


async def test_a_reversal_repeated_at_the_same_total_reports_no_change(db_session):
    """A second, distinct notification saying the same thing as the first.

    Not a duplicate - it is a different event id - and not a change either.
    Recording it as `applied` would say money moved when none did.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)

    await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), transaction="1001", refunded_cents=9900),
    )
    outcome = await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), transaction="1002", refunded_cents=9900),
    )

    assert outcome == NO_CHANGE
    assert payment.refunded_amount == Decimal("99.00")


async def test_a_refunded_payment_cannot_be_collected_again(db_session):
    """The attack, and the ordinary accident, that the transition table stops.

    A signed callback claiming success about a refunded payment would settle
    the invoice a second time - leaving a customer with their money back and a
    paid invoice, which is the product for free.
    """
    tenant = await _tenant(db_session)
    invoice, payment = await _paid(db_session, tenant)
    await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=9900),
    )
    assert payment.status is PaymentStatus.REFUNDED

    collected = {
        **_reversal(reference=str(payment.id), transaction="777"),
        "is_refunded": False,
        "is_voided": False,
    }
    outcome = await _apply(db_session, tenant, collected)

    assert outcome == REFUSED
    assert payment.status is PaymentStatus.REFUNDED
    assert invoice.amount_paid == Decimal("0.00")


async def test_a_void_is_recorded_as_a_void_rather_than_a_refund(db_session):
    """Both give the money back; only one of them had settled first.

    An operator reconciling against a bank statement needs to know which.
    """
    tenant = await _tenant(db_session)
    _, payment = await _paid(db_session, tenant)

    await _apply(
        db_session,
        tenant,
        _reversal(reference=str(payment.id), refunded_cents=9900, voided=True),
    )
    await db_session.flush()

    events = (
        (
            await db_session.execute(
                select(PaymentEvent).where(PaymentEvent.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    )
    assert [event.event_type for event in events] == ["transaction.voided"]
    assert payment.status is PaymentStatus.REFUNDED
