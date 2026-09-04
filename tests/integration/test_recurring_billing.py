"""Automatic renewal: what it charges, and far more importantly what it does not.

An automatic debit is the one billing operation where a bug takes money from
somebody who is not watching. So most of this file is about the refusals, and
the one that matters most has its own test: **a cancelled workspace is never
charged.** Everything else here is recoverable by a refund and an apology; that
one is the failure customers do not forgive.

The provider runs for real against `httpx.MockTransport` - the real intention
body is built, the real pay request is made, the real response parsed. What is
*not* real is the merchant capability: charging a saved card requires a Moto
integration that Paymob issues per merchant, so the tests configure one and the
account this was developed against does not have one. That distinction is the
whole reason `can_charge_saved_methods` exists as a property rather than an
exception.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
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
    ATTEMPTS_EXHAUSTED,
    MAX_COLLECTION_ATTEMPTS,
    NO_CARD,
    NOT_COLLECTIBLE,
    NOT_DUE,
    NOT_SERVING,
    NOT_SUPPORTED,
    OUTCOME_UNKNOWN,
    PROVIDER_REFUSED,
    RecurringService,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
MOTO_INTEGRATION = 9900001
CARD_TOKEN = "3f22ce8a4e77125c70f0bc69830e34c36df469351e2fa6be76428be4"


def _transport(
    seen: list[dict[str, Any]] | None = None, *, fail: bool = False
) -> httpx.MockTransport:
    """Paymob's two-step merchant-initiated charge, faked at the socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if seen is not None:
            seen.append({"url": str(request.url), "body": body})
        if "intention" in str(request.url):
            return httpx.Response(
                201,
                json={
                    "id": "pi_auto_1",
                    "client_secret": "csk_auto_1",
                    "payment_keys": [{"key": "a-payment-token", "integration": MOTO_INTEGRATION}],
                },
            )
        if fail:
            return httpx.Response(400, json={"detail": "declined"})
        return httpx.Response(200, json={"id": 700000123, "pending": False, "success": True})

    return httpx.MockTransport(handler)


def _provider(
    transport: httpx.MockTransport | None = None,
    *,
    moto: int | None = MOTO_INTEGRATION,
) -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret="a-test-hmac-secret",
        integration_ids=[4097558],
        moto_integration_id=moto,
        transport=transport if transport is not None else _transport(),
    )


async def _workspace(
    session: AsyncSession,
    *,
    slug: str = "acme",
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    with_card: bool = True,
    card_status: PaymentMethodStatus = PaymentMethodStatus.ACTIVE,
) -> tuple[Tenant, Subscription, Invoice]:
    """A workspace mid-period with an unpaid renewal waiting."""
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
        issued_at=NOW - timedelta(days=5),
        lines=[],
        collection_attempts=0,
    )
    session.add(invoice)

    if with_card:
        session.add(
            PaymentMethod(
                tenant_id=tenant.id,
                provider="paymob",
                provider_token=f"{CARD_TOKEN}-{uuid.uuid4().hex[:6]}",
                provider_token_id="15978654",
                masked_pan="xxxx-xxxx-xxxx-2346",
                brand="MasterCard",
                status=card_status,
                is_default=card_status is PaymentMethodStatus.ACTIVE,
            )
        )
    await session.flush()
    return tenant, subscription, invoice


def _service(
    session: AsyncSession,
    tenant: Tenant,
    *,
    transport: httpx.MockTransport | None = None,
    moto: int | None = MOTO_INTEGRATION,
) -> RecurringService:
    return RecurringService(
        session,
        tenant_id=tenant.id,
        provider=_provider(transport, moto=moto),
    )


# ----------------------------------------------------------------- charging


async def test_a_due_renewal_is_taken_from_the_saved_card(db_session: AsyncSession) -> None:
    """The happy path, and note what it does *not* assert.

    Nothing here is paid. The charge request went out; whether money moved is
    decided later by a signed callback, on exactly the same settlement path a
    customer-initiated payment uses.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.charged
    assert invoice.status is InvoiceStatus.OPEN, "a request is not a settlement"

    payment = await db_session.get(Payment, outcome.payment_id)
    assert payment is not None
    assert payment.status is PaymentStatus.PENDING
    assert payment.is_automatic is True
    assert payment.payment_method_id is not None
    assert payment.provider_intent_reference == "700000123"


async def test_the_charge_uses_the_moto_integration_and_our_own_reference(
    db_session: AsyncSession,
) -> None:
    """Two documented requirements, both easy to get wrong and both invisible
    until a live merchant account rejects them."""
    tenant, subscription, invoice = await _workspace(db_session)
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    intention = next(item for item in seen if "intention" in item["url"])
    pay = next(item for item in seen if item["url"].endswith("/payments/pay"))

    assert intention["body"]["payment_methods"] == [MOTO_INTEGRATION]
    assert intention["body"]["special_reference"] == str(outcome.payment_id)
    assert pay["body"]["source"]["subtype"] == "TOKEN"
    assert pay["body"]["payment_token"] == "a-payment-token"


async def test_the_card_token_is_sent_and_never_the_card_number(db_session: AsyncSession) -> None:
    """There is no card number to send, which is the point of the token."""
    tenant, subscription, invoice = await _workspace(db_session)
    seen: list[dict[str, Any]] = []

    await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    method = (
        await db_session.execute(select(PaymentMethod).where(PaymentMethod.tenant_id == tenant.id))
    ).scalar_one()
    pay = next(item for item in seen if item["url"].endswith("/payments/pay"))
    assert pay["body"]["source"]["identifier"] == method.provider_token
    assert "5123456789012346" not in json.dumps(seen)


# ----------------------------------------------------------------- refusals


async def test_a_cancelled_workspace_is_never_charged(db_session: AsyncSession) -> None:
    """The refusal that matters most in the whole subsystem.

    Debiting somebody who has cancelled is not a recoverable billing error. It
    is the one failure that ends a relationship regardless of how quickly the
    money is returned.
    """
    tenant, subscription, invoice = await _workspace(
        db_session,
        status=SubscriptionStatus.CANCELLED,
    )
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert not outcome.charged
    assert outcome.reason == NOT_SERVING
    assert seen == [], "no request may reach the provider at all"
    assert invoice.collection_attempts == 0


async def test_an_expired_workspace_is_never_charged(db_session: AsyncSession) -> None:
    """A trial that lapsed is not a customer who agreed to pay."""
    tenant, subscription, invoice = await _workspace(
        db_session,
        status=SubscriptionStatus.EXPIRED,
    )
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NOT_SERVING
    assert seen == []


async def test_a_subscription_that_vanished_is_never_charged(db_session: AsyncSession) -> None:
    """An invoice whose subscription is gone has nobody to bill."""
    tenant, _, invoice = await _workspace(db_session)
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=None,
        now=NOW,
    )

    assert outcome.reason == NOT_SERVING
    assert seen == []


async def test_a_workspace_with_no_card_is_not_charged(db_session: AsyncSession) -> None:
    tenant, subscription, invoice = await _workspace(db_session, with_card=False)

    outcome = await _service(db_session, tenant).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NO_CARD


async def test_a_revoked_card_is_not_charged(db_session: AsyncSession) -> None:
    """Removing a card must actually stop renewals, not merely hide it."""
    tenant, subscription, invoice = await _workspace(
        db_session,
        card_status=PaymentMethodStatus.REVOKED,
    )
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NO_CARD
    assert seen == []


async def test_a_paid_invoice_is_not_charged_again(db_session: AsyncSession) -> None:
    tenant, subscription, invoice = await _workspace(db_session)
    invoice.status = InvoiceStatus.PAID
    invoice.amount_paid = Decimal("25.00")
    await db_session.flush()

    outcome = await _service(db_session, tenant).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NOT_COLLECTIBLE


async def test_nothing_is_charged_without_the_merchant_capability(db_session: AsyncSession) -> None:
    """No Moto integration configured, so no automatic collection happens.

    This is the state the account this was built against is actually in, and it
    is a supported one: renewals fall back to invoicing the customer, which is
    how the product billed before saved cards existed.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen), moto=None).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NOT_SUPPORTED
    assert seen == []
    assert invoice.collection_attempts == 0


async def test_the_service_reports_whether_it_can_collect_at_all(db_session: AsyncSession) -> None:
    tenant, _, _ = await _workspace(db_session)

    assert _service(db_session, tenant).available is True
    assert _service(db_session, tenant, moto=None).available is False
    assert RecurringService(db_session, tenant_id=tenant.id).available is False


# ------------------------------------------------------------------ retries


async def test_a_refused_charge_records_a_failed_attempt(db_session: AsyncSession) -> None:
    """The invoice stays open and the workspace is not marked paid."""
    tenant, subscription, invoice = await _workspace(db_session)

    outcome = await _service(db_session, tenant, transport=_transport(fail=True)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert not outcome.charged
    assert outcome.reason == PROVIDER_REFUSED
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")

    payment = await db_session.get(Payment, outcome.payment_id)
    assert payment is not None
    assert payment.status is PaymentStatus.FAILED


async def test_attempts_are_counted_and_spaced_out(db_session: AsyncSession) -> None:
    """A declined card is asked again later, not immediately.

    The commonest recoverable decline is a temporary funds problem, and a
    customer needs days to fix it rather than minutes.
    """
    tenant, subscription, invoice = await _workspace(db_session)

    await _service(db_session, tenant, transport=_transport(fail=True)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert invoice.collection_attempts == 1
    assert invoice.next_collection_at is not None
    assert invoice.next_collection_at > NOW


async def test_an_attempt_before_its_time_is_not_made(db_session: AsyncSession) -> None:
    """The sweep runs every ten minutes; retries must not."""
    tenant, subscription, invoice = await _workspace(db_session)
    invoice.collection_attempts = 1
    invoice.next_collection_at = NOW + timedelta(days=1)
    await db_session.flush()
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == NOT_DUE
    assert seen == []


async def test_the_attempt_budget_is_bounded(db_session: AsyncSession) -> None:
    """A card that has declined three times will not work on the fourth.

    Retrying for ever is also how a merchant account gets looked at by the
    processor's risk team.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    invoice.collection_attempts = MAX_COLLECTION_ATTEMPTS
    invoice.next_collection_at = None
    await db_session.flush()
    seen: list[dict[str, Any]] = []

    outcome = await _service(db_session, tenant, transport=_transport(seen)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert outcome.reason == ATTEMPTS_EXHAUSTED
    assert seen == []


async def test_the_last_attempt_schedules_nothing_further(db_session: AsyncSession) -> None:
    """`next_collection_at` becomes NULL so "we have stopped" is queryable."""
    tenant, subscription, invoice = await _workspace(db_session)
    invoice.collection_attempts = MAX_COLLECTION_ATTEMPTS - 1
    await db_session.flush()

    await _service(db_session, tenant, transport=_transport(fail=True)).collect(
        invoice,
        subscription=subscription,
        now=NOW,
    )

    assert invoice.collection_attempts == MAX_COLLECTION_ATTEMPTS
    assert invoice.next_collection_at is None


async def test_a_timed_out_charge_is_unknown_rather_than_failed(
    db_session: AsyncSession,
) -> None:
    """No answer is not an answer, and the attempt is counted either way.

    The pay request - the one that moves money - times out here. Two things
    have to be true afterwards and they used to conflict.

    The attempt is counted, because a request that was not answered is not a
    request that was not carried out, and a provider that never answers must
    not be retried for ever. That was already the case.

    And the payment stays `pending`, in `requested`, rather than being written
    off as failed. Calling this a failure was the old behaviour and it was a
    second bug wearing the first one's clothes: `failed -> succeeded` is not a
    legal transition, so the callback that eventually reported the charge
    Paymob had in fact taken could never settle the invoice, and the customer
    would be chased for money they had already paid (ADR-088).
    """
    tenant, subscription, invoice = await _workspace(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        if "intention" in str(request.url):
            return httpx.Response(
                201,
                json={"id": "pi_1", "client_secret": "c", "payment_keys": [{"key": "k"}]},
            )
        raise httpx.ReadTimeout("no answer", request=request)

    outcome = await _service(
        db_session,
        tenant,
        transport=httpx.MockTransport(handler),
    ).collect(invoice, subscription=subscription, now=NOW)

    assert outcome.reason == OUTCOME_UNKNOWN
    assert invoice.collection_attempts == 1

    payment = await db_session.get(Payment, outcome.payment_id)
    assert payment is not None
    assert payment.status is PaymentStatus.PENDING, "an unanswered charge is not a refused one"
    assert payment.collection_state is CollectionState.REQUESTED
    assert payment.is_unresolved_collection, "reconciliation has to own this"


async def test_two_sweeps_cannot_both_make_the_same_attempt(db_session: AsyncSession) -> None:
    """The idempotency key names the invoice and the attempt number.

    Two workers reaching the same invoice at once would otherwise both charge.
    Here the second finds the attempt already claimed and does nothing.
    """
    tenant, subscription, invoice = await _workspace(db_session)
    service = _service(db_session, tenant)

    first = await service.collect(invoice, subscription=subscription, now=NOW)
    # Rewind the schedule so the second call is not merely refused as not-due:
    # the claim itself has to be what stops it.
    invoice.collection_attempts -= 1
    invoice.next_collection_at = None
    await db_session.flush()
    second = await service.collect(invoice, subscription=subscription, now=NOW)

    assert first.charged
    assert not second.charged
    assert second.reason == NOT_DUE

    payments = (
        (await db_session.execute(select(Payment).where(Payment.tenant_id == tenant.id)))
        .scalars()
        .all()
    )
    assert len(payments) == 1, "one attempt, however many workers looked at it"
