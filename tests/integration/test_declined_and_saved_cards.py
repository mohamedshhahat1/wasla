"""Declined payments, the pending step before one lands, and saved cards.

Three things that share a theme: **nothing here may grant anybody anything.** A
declined callback must leave the workspace exactly as it found it, a pending one
must settle nothing, and a saved card must attach only to the workspace whose
checkout produced it.

The declined path is easy to get subtly wrong in a way no happy-path test
notices. The dangerous version is not "a decline crashes" - it is a decline
that quietly settles an invoice, or leaves a subscription active because
nothing checked, and the customer gets the product without paying. So these
assert the *absence* of effects rather than the presence of an error.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider, hmac_signature, token_hmac_signature
from app.services.checkout_service import APPLIED, DUPLICATE, NO_CHANGE, CheckoutService
from app.services.entitlement_service import EntitlementService
from app.services.payment_method_service import PaymentMethodService, remember_saved_method

pytestmark = pytest.mark.integration

HMAC_SECRET = "a-test-hmac-secret"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _provider() -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
    )


def _transaction(*, reference: str | None, transaction: int = 800000001, **overrides) -> dict:
    body = {
        "id": transaction,
        "pending": False,
        "amount_cents": 2500,
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": 4097558,
        "has_parent_transaction": False,
        "order": {"id": 1, "merchant_order_id": reference},
        "created_at": "2026-08-29T11:33:44.592345",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    body.update(overrides)
    return body


async def _apply(session, tenant, transaction: dict) -> str:
    payload = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    event = _provider().verify_callback(
        payload=payload,
        signature=hmac_signature(transaction, secret=HMAC_SECRET),
    )
    return await CheckoutService(session, tenant_id=tenant.id, provider=_provider()).apply(event)


async def _workspace(session, *, slug: str = "acme", status=SubscriptionStatus.PAST_DUE):
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
        status=status,
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
        lines=[],
    )
    session.add(invoice)
    await session.flush()

    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=Decimal("25.00"),
        currency="EGP",
        provider="paymob",
        refunded_amount=Decimal("0.00"),
    )
    session.add(payment)
    await session.flush()
    return tenant, subscription, invoice, payment


# ----------------------------------------------------------------- declined


async def test_a_declined_payment_changes_nothing_it_should_not(db_session):
    """The whole decline contract in one place.

    Asserting absences rather than an error, because the dangerous bug is not a
    crash - it is a decline that settles an invoice or leaves a subscription
    served, and hands somebody the product for nothing.
    """
    tenant, subscription, invoice, payment = await _workspace(db_session)

    outcome = await _apply(
        db_session,
        tenant,
        _transaction(
            reference=str(payment.id),
            success=False,
            error_occured=True,
            data={"message": "Insufficient funds"},
        ),
    )

    assert outcome == APPLIED, "the event is recorded; what it did is the assertion below"
    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == "Insufficient funds"
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.paid_at is None
    # The subscription was behind before and is still behind. A failed payment
    # is not a reason to serve somebody.
    assert subscription.status is SubscriptionStatus.PAST_DUE


async def test_a_decline_does_not_grant_paid_entitlements(db_session):
    """Read through the real entitlement service rather than inferred.

    A workspace whose payment failed keeps whatever its subscription already
    entitled it to, and gains nothing from the attempt.
    """
    tenant, _, _, payment = await _workspace(db_session, status=SubscriptionStatus.CANCELLED)

    await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=False, error_occured=True),
    )

    entitlements = await EntitlementService(db_session, tenant_id=tenant.id).snapshot()
    agents = next(item for item in entitlements if item.key is LimitKey.AGENTS)
    # Cancelled means not served, so the plan's five agents are not granted -
    # a failed payment certainly does not restore them.
    assert agents.limit != 5


async def test_a_decline_is_recorded_as_its_own_event(db_session):
    """`transaction.failed`, distinct from a success on the same transaction."""
    tenant, _, _, payment = await _workspace(db_session)

    await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=False, error_occured=True),
    )
    await db_session.flush()

    from app.db.models.payment_event import PaymentEvent

    event = (
        await db_session.execute(select(PaymentEvent).where(PaymentEvent.payment_id == payment.id))
    ).scalar_one()
    assert event.event_type == "transaction.failed"
    assert event.provider_event_id.endswith(":failed")


async def test_a_repeated_decline_is_idempotent(db_session):
    tenant, _, invoice, payment = await _workspace(db_session)
    declined = _transaction(reference=str(payment.id), success=False, error_occured=True)

    first = await _apply(db_session, tenant, declined)
    second = await _apply(db_session, tenant, declined)

    assert (first, second) == (APPLIED, DUPLICATE)
    assert invoice.amount_paid == Decimal("0.00")


async def test_a_declined_payment_cannot_later_succeed(db_session):
    """`failed -> succeeded` is refused, so a late success cannot resurrect it.

    A customer retrying produces another attempt and another row; this row
    keeps the decline, which is what a dispute turns on.
    """
    tenant, _, invoice, payment = await _workspace(db_session)
    await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=False, error_occured=True),
    )
    assert payment.status is PaymentStatus.FAILED

    outcome = await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), transaction=800000002),
    )

    assert outcome == "refused"
    assert payment.status is PaymentStatus.FAILED
    assert invoice.status is InvoiceStatus.OPEN


# ------------------------------------------------------------------ pending


async def test_a_pending_callback_settles_nothing(db_session):
    """In flight is not collected, however cheerful the flags look."""
    tenant, subscription, invoice, payment = await _workspace(db_session)

    outcome = await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=True, pending=True),
    )

    # The attempt was already pending, so the provider said nothing new.
    assert outcome == NO_CHANGE
    assert payment.status is PaymentStatus.PENDING
    assert invoice.status is InvoiceStatus.OPEN
    assert subscription.status is SubscriptionStatus.PAST_DUE


async def test_pending_then_success_are_two_events_and_settle_once(db_session):
    """The 3-D Secure sequence, and the reason event ids are composite.

    Keying idempotency on the transaction id alone would file the success as a
    duplicate of the pending notification, and the customer's money would be
    taken with nothing recorded.
    """
    tenant, subscription, invoice, payment = await _workspace(db_session)

    await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=True, pending=True),
    )
    settled = await _apply(
        db_session,
        tenant,
        _transaction(reference=str(payment.id), success=True, pending=False),
    )
    await db_session.flush()

    assert settled == APPLIED
    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.amount_paid == Decimal("25.00")
    assert subscription.status is SubscriptionStatus.ACTIVE

    from app.db.models.payment_event import PaymentEvent

    events = (
        (
            await db_session.execute(
                select(PaymentEvent).where(PaymentEvent.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    )
    assert {event.event_type for event in events} == {
        "transaction.pending",
        "transaction.succeeded",
    }


# -------------------------------------------------------------- saved cards


def _token(*, order: str, token: str = "tok-aaaa") -> dict:  # noqa: S107 - a fixture handle
    return {
        "id": 15978654,
        "token": token,
        "masked_pan": "xxxx-xxxx-xxxx-2346",
        "merchant_id": 1053928,
        "card_subtype": "MasterCard",
        "created_at": "2026-08-29T13:28:31.015314",
        "email": "owner@example.com",
        "order_id": order,
    }


async def _save(session, tenant, token: dict):
    payload = json.dumps({"type": "TOKEN", "obj": token}).encode("utf-8")
    saved = _provider().verify_token_callback(
        payload=payload,
        signature=token_hmac_signature(token, secret=HMAC_SECRET),
    )
    return await remember_saved_method(
        session,
        tenant_id=tenant.id,
        provider="paymob",
        saved=saved,
    )


async def test_a_saved_card_stores_a_token_and_no_card_number(db_session):
    tenant, _, _, _ = await _workspace(db_session)

    method, created = await _save(db_session, tenant, _token(order="ord-1"))

    assert created
    assert method.provider_token == "tok-aaaa"
    assert method.masked_pan == "xxxx-xxxx-xxxx-2346"
    assert method.brand == "MasterCard"
    # The first card a workspace saves becomes the one renewals use.
    assert method.is_default is True
    columns = {column.name for column in PaymentMethod.__table__.columns}
    assert not (columns & {"pan", "card_number", "cvv", "expiry"})


async def test_a_repeated_saved_card_notification_creates_one_card(db_session):
    """Providers retry these exactly as they retry payment callbacks."""
    tenant, _, _, _ = await _workspace(db_session)
    token = _token(order="ord-2")

    first, created_first = await _save(db_session, tenant, token)
    second, created_second = await _save(db_session, tenant, token)

    assert created_first and not created_second
    assert first.id == second.id


async def test_a_second_card_does_not_silently_take_over_renewals(db_session):
    """Somebody paying once with another card has not changed their billing."""
    tenant, _, _, _ = await _workspace(db_session)

    first, _ = await _save(db_session, tenant, _token(order="ord-3", token="tok-first"))
    second, _ = await _save(db_session, tenant, _token(order="ord-4", token="tok-second"))

    assert first.is_default is True
    assert second.is_default is False


async def test_choosing_a_card_moves_the_default_exactly_once(db_session):
    tenant, _, _, _ = await _workspace(db_session)
    first, _ = await _save(db_session, tenant, _token(order="ord-5", token="tok-a"))
    second, _ = await _save(db_session, tenant, _token(order="ord-6", token="tok-b"))
    service = PaymentMethodService(db_session, tenant_id=tenant.id)

    await service.make_default(second.id)

    assert first.is_default is False
    assert second.is_default is True
    assert (await service.default_method()).id == second.id


async def test_removing_a_card_stops_it_being_used_without_erasing_it(db_session):
    """A payment collected with a card should still name that card afterwards."""
    tenant, _, _, _ = await _workspace(db_session)
    method, _ = await _save(db_session, tenant, _token(order="ord-7"))
    service = PaymentMethodService(db_session, tenant_id=tenant.id)

    await service.revoke(method.id, now=NOW)

    assert method.status is PaymentMethodStatus.REVOKED
    assert method.is_default is False
    assert await service.default_method() is None
    assert await db_session.get(PaymentMethod, method.id) is not None


async def test_another_workspaces_card_cannot_be_touched(db_session):
    """Not-found rather than forbidden, on an object that can be charged."""
    from app.core.exceptions import NotFoundError

    acme, _, _, _ = await _workspace(db_session, slug="acme")
    globex, _, _, _ = await _workspace(db_session, slug="globex")
    method, _ = await _save(db_session, globex, _token(order="ord-8"))
    service = PaymentMethodService(db_session, tenant_id=acme.id)

    with pytest.raises(NotFoundError):
        await service.make_default(method.id)
    with pytest.raises(NotFoundError):
        await service.revoke(method.id)


async def test_a_card_saved_by_one_workspace_is_invisible_to_another(db_session):
    acme, _, _, _ = await _workspace(db_session, slug="acme")
    globex, _, _, _ = await _workspace(db_session, slug="globex")
    await _save(db_session, globex, _token(order="ord-9"))

    assert await PaymentMethodService(db_session, tenant_id=acme.id).list_methods() == []
    assert await PaymentMethodService(db_session, tenant_id=globex.id).list_methods() != []
