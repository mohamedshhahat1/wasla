"""What a refund does to the thing the money bought.

Every other refund test in this repository asks what happened to the *payment*:
its status, its returned balance, the invoice behind it. None of them asks the
question a customer would - **do I still have the plan I just got my money back
for?** - and the answer, until the fix these tests pin, was yes, permanently.

The distinction is the whole point of the file. A payment-state test and an
entitlement test can both be green while the product is being given away, and
`test_paymob_refunds.py` was: its fixtures build an invoice with no
subscription at all, so there is nothing for a reversal to withdraw and nothing
to notice when it does not.

Everything here runs the real services against real PostgreSQL: a real
`Plan` catalogue, a real `Subscription`, the real `CheckoutService.apply`
behind a real Paymob HMAC, and `EntitlementService` reading the limits back the
way a request does. Nothing simulates "subscription state"; the rows are the
state.
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

from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.invoice import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.tenant import Tenant
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.services.checkout_service import APPLIED, DUPLICATE, CheckoutService
from app.services.entitlement_service import EntitlementService

pytestmark = pytest.mark.integration

HMAC_SECRET = "a-test-hmac-secret"
FREE_PLAN = "starter"
PAID_PLAN = "pro"
PAID_TRANSACTION = "192036465"
SECOND_TRANSACTION = "192036999"
OTHER_PAID_PLAN = "business"
FREE_AGENTS = 1
PAID_AGENTS = 20
OTHER_PAID_AGENTS = 50
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _provider() -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": 1, "success": True})
        ),
    )


async def _catalogue(session: AsyncSession) -> dict[str, Plan]:
    """Three plans: the free default, the one that gets bought, and a third.

    The third exists so "the workspace has since moved on" can mean somewhere
    other than the free plan. Without it a wrong withdrawal is invisible - it
    would move a workspace to the plan it is already on, `change_plan` would
    refuse, and the containment around it would swallow the refusal. That is a
    test which cannot fail, and it is the shape this file exists to avoid.

    The catalogue is platform-wide - `plans.code` is unique across the
    installation, not per workspace - so a second workspace in the same test
    shares these rows rather than raising on the constraint.
    """
    wanted = {
        FREE_PLAN: ("Starter", "0.00", FREE_AGENTS),
        PAID_PLAN: ("Pro", "99.00", PAID_AGENTS),
        OTHER_PAID_PLAN: ("Business", "249.00", OTHER_PAID_AGENTS),
    }
    plans = {
        plan.code: plan
        for plan in (await session.execute(select(Plan))).scalars().all()
        if plan.code in wanted
    }
    for code, (name, price, agents) in wanted.items():
        if code in plans:
            continue
        plan = Plan(
            code=code,
            name=name,
            price=Decimal(price),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.AGENTS.value: agents},
        )
        session.add(plan)
        plans[code] = plan
    await session.flush()
    return plans


async def _workspace(
    session: AsyncSession,
    *,
    slug: str = "acme",
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
) -> tuple[Tenant, Subscription, dict[str, Plan]]:
    """A workspace as registration leaves it: on the free plan, subscribed."""
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    plans = await _catalogue(session)
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plans[FREE_PLAN].id,
        status=status,
        current_period_start=NOW,
        current_period_end=NOW + timedelta(days=30),
    )
    session.add(subscription)
    await session.flush()
    return tenant, subscription, plans


async def _bought(
    session: AsyncSession,
    tenant: Tenant,
    subscription: Subscription,
    *,
    plan_code: str = PAID_PLAN,
    amount: str = "99.00",
    transaction: str = PAID_TRANSACTION,
    period_start: datetime = NOW,
    period_end: datetime | None = None,
) -> tuple[Invoice, Payment]:
    """An invoice and a pending attempt, exactly as a checkout writes them.

    Built through `Invoice(...)` rather than through `CheckoutService.start`
    only because `start` needs a live provider session; every field below is
    what `_open_invoice` and `_new_attempt` put there, including the absent
    `issued_at` that F-1 turns on.
    """
    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        plan_code=plan_code,
        amount_due=Decimal(amount),
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=period_start,
        period_end=period_end if period_end is not None else period_start + timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        status=PaymentStatus.PENDING,
        amount=Decimal(amount),
        currency="EGP",
        provider="paymob",
        # NULL, as a hosted checkout leaves it: the collection protocol is for
        # automatic debits and nothing here is one.
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


def _transaction(
    *,
    reference: str,
    transaction: str,
    amount_cents: int,
    success: bool = True,
    refunded_cents: int | None = None,
    voided: bool = False,
    is_refund: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": int(transaction),
        "pending": False,
        "amount_cents": amount_cents,
        "success": success,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": voided,
        "is_refunded": is_refund,
        "is_3d_secure": True,
        "integration_id": 4097558,
        "has_parent_transaction": False,
        "parent_transaction": None,
        "order": {"id": 217503754, "merchant_order_id": reference},
        "created_at": "2026-08-29T11:33:44.592345",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }
    if refunded_cents is not None:
        body["refunded_amount_cents"] = refunded_cents
    return body


async def _apply(
    session: AsyncSession,
    tenant: Tenant,
    transaction: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Run one signed callback through the real service, as the webhook does."""
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    signature = hmac_signature(transaction, secret=HMAC_SECRET)
    event = _provider().verify_callback(payload=body, signature=signature)
    service = CheckoutService(
        session,
        tenant_id=tenant.id,
        provider=_provider(),
        default_plan_code=FREE_PLAN,
    )
    return await service.apply(event, now=now if now is not None else NOW)


async def _settle(
    session: AsyncSession,
    tenant: Tenant,
    payment: Payment,
    *,
    transaction: str = PAID_TRANSACTION,
    now: datetime | None = None,
) -> str:
    return await _apply(
        session,
        tenant,
        _transaction(
            reference=str(payment.id),
            transaction=transaction,
            amount_cents=int(payment.amount * 100),
        ),
        now=now,
    )


async def _reverse(
    session: AsyncSession,
    tenant: Tenant,
    payment: Payment,
    *,
    returned: str | None = None,
    transaction: str = PAID_TRANSACTION,
    now: datetime | None = None,
) -> str:
    """A refund notification for `payment`, cumulative as Paymob reports it."""
    total = Decimal(returned) if returned is not None else payment.amount
    return await _apply(
        session,
        tenant,
        _transaction(
            reference=str(payment.id),
            transaction=transaction,
            amount_cents=int(payment.amount * 100),
            refunded_cents=int(total * 100),
            is_refund=True,
        ),
        now=now,
    )


async def _agents_allowed(session: AsyncSession, tenant: Tenant) -> int:
    """The limit a request would be given, read the way a request reads it."""
    entitlement = await EntitlementService(
        session,
        tenant_id=tenant.id,
        default_plan_code=FREE_PLAN,
    ).check(LimitKey.AGENTS)
    return entitlement.limit if entitlement.limit is not None else -1


async def _plan_code(session: AsyncSession, subscription: Subscription) -> str:
    await session.refresh(subscription)
    plan = await session.get(Plan, subscription.plan_id)
    assert plan is not None
    return plan.code


# ------------------------------------------------------- the reversal itself


async def test_a_full_refund_withdraws_the_plan_the_payment_bought(
    db_session: AsyncSession,
) -> None:
    """F-1. The money goes back, so the thing it bought goes back too.

    This is the finding in one test. Before the fix every assertion below the
    first two passed *inverted*: the payment was refunded, the invoice reopened,
    and the workspace kept Pro for ever.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)

    assert await _settle(db_session, tenant, payment) == APPLIED
    assert await _plan_code(db_session, subscription) == PAID_PLAN
    assert await _agents_allowed(db_session, tenant) == PAID_AGENTS

    assert await _reverse(db_session, tenant, payment) == APPLIED

    await db_session.refresh(payment)
    await db_session.refresh(invoice)
    assert payment.status is PaymentStatus.REFUNDED
    assert payment.refunded_amount == Decimal("99.00")
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("0.00")
    assert await _plan_code(db_session, subscription) == FREE_PLAN
    assert await _agents_allowed(db_session, tenant) == FREE_AGENTS


async def test_the_withdrawal_is_audited_as_a_reversal_and_not_as_a_choice(
    db_session: AsyncSession,
) -> None:
    """An operator reading the trail must see why the plan moved.

    `change_plan` writes `SUBSCRIPTION_PLAN_CHANGED` whoever calls it, which on
    its own reads as the customer having picked Starter. The reversal entry
    beside it is what says the platform took it back, and names the invoice.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    await _reverse(db_session, tenant, payment)
    await db_session.flush()

    entries = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant.id)
                .where(AuditLog.action == AuditAction.SUBSCRIPTION_PLAN_WITHDRAWN)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    meta = entries[0].meta or {}
    assert meta["invoice_id"] == str(invoice.id)
    assert meta["plan_code"] == PAID_PLAN
    assert meta["reason"] == "settlement_reversed"
    assert entries[0].target_label == FREE_PLAN


async def test_a_partial_refund_leaves_the_plan_alone(
    db_session: AsyncSession,
) -> None:
    """Losing a month of Pro over one unit returned is not the right trade.

    The rule is about cover, not about the refund's existence: money is still
    on the invoice, so the customer still bought something. What changes is
    that they now owe the balance - which is the next test.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    assert await _reverse(db_session, tenant, payment, returned="1.00") == APPLIED

    await db_session.refresh(payment)
    await db_session.refresh(invoice)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.amount_paid == Decimal("98.00")
    assert await _plan_code(db_session, subscription) == PAID_PLAN
    assert await _agents_allowed(db_session, tenant) == PAID_AGENTS


async def test_a_reversal_that_leaves_a_balance_makes_the_invoice_chaseable(
    db_session: AsyncSession,
) -> None:
    """F-1's second half. A bill nobody can chase is not a bill.

    A checkout invoice is deliberately not issued - an abandoned payment page
    is not a debt. A part-reversed one is: the customer has the plan and owes
    the difference, and `issued_at` is what both dunning sweeps read.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)
    await db_session.refresh(invoice)
    assert invoice.issued_at is None, "a checkout invoice starts unissued"

    await _reverse(db_session, tenant, payment, returned="40.00", now=NOW)

    await db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.OPEN
    assert invoice.issued_at == NOW


async def test_a_full_reversal_does_not_start_chasing_the_refunded_customer(
    db_session: AsyncSession,
) -> None:
    """The control for the test above, and the reason it is conditional.

    Somebody who asked for their money back and got it owes nothing. Issuing
    the reopened invoice would put them into dunning for the sum they were just
    refunded, which is a worse bug than the one being fixed.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    await _reverse(db_session, tenant, payment)

    await db_session.refresh(invoice)
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.issued_at is None


# ------------------------------------------------- what must not be withdrawn


async def test_another_settled_invoice_for_the_same_plan_keeps_it(
    db_session: AsyncSession,
) -> None:
    """ "Granted solely by this payment" is a query, not an assumption.

    A workspace that paid for August and again for September, then refunded
    August, has bought September. Downgrading on the strength of the reversed
    invoice alone would take a plan the customer is paid up on.
    """
    tenant, subscription, _ = await _workspace(db_session)
    august, august_payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, august_payment)

    september, september_payment = await _bought(
        db_session,
        tenant,
        subscription,
        transaction=SECOND_TRANSACTION,
        period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=29),
    )
    await _settle(db_session, tenant, september_payment, transaction=SECOND_TRANSACTION)
    assert await _plan_code(db_session, subscription) == PAID_PLAN

    assert await _reverse(db_session, tenant, august_payment) == APPLIED

    await db_session.refresh(august)
    assert august.amount_paid == Decimal("0.00")
    assert await _plan_code(db_session, subscription) == PAID_PLAN
    assert await _agents_allowed(db_session, tenant) == PAID_AGENTS


async def test_a_settled_invoice_for_a_period_that_has_ended_does_not_count(
    db_session: AsyncSession,
) -> None:
    """The control for the test above: cover means cover *now*.

    Last month's paid invoice is not a reason to keep this month's plan, or the
    check would be "have you ever paid" and every refund after the first would
    be free.
    """
    tenant, subscription, _ = await _workspace(db_session)
    old, old_payment = await _bought(
        db_session,
        tenant,
        subscription,
        transaction=SECOND_TRANSACTION,
        period_start=NOW - timedelta(days=60),
        period_end=NOW - timedelta(days=30),
    )
    await _settle(db_session, tenant, old_payment, transaction=SECOND_TRANSACTION)

    current, current_payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, current_payment)
    assert await _plan_code(db_session, subscription) == PAID_PLAN

    await _reverse(db_session, tenant, current_payment)

    assert old.amount_paid == Decimal("99.00")
    assert await _plan_code(db_session, subscription) == FREE_PLAN


async def test_refunding_a_plan_the_workspace_has_since_left_changes_nothing(
    db_session: AsyncSession,
) -> None:
    """A reversal withdraws what it granted, never what somebody else did.

    The workspace bought Pro, then moved to Business. The late refund of the
    Pro payment must not reach past that decision and knock them down to
    Starter - a plan they neither chose nor got a refund for.

    Deliberately a *third* plan rather than the free one. Moving to Starter
    first would make a wrong withdrawal unobservable: it would target the plan
    the workspace already holds, `change_plan` would refuse it as a no-op, and
    the containment would swallow the refusal. The mutation that deletes the
    correlation check survives that version of this test and fails this one.
    """
    tenant, subscription, plans = await _workspace(db_session)
    _, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)
    subscription.plan_id = plans[OTHER_PAID_PLAN].id
    await db_session.flush()

    await _reverse(db_session, tenant, payment)
    await db_session.flush()

    withdrawals = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant.id)
                .where(AuditLog.action == AuditAction.SUBSCRIPTION_PLAN_WITHDRAWN)
            )
        )
        .scalars()
        .all()
    )
    assert withdrawals == []
    assert await _plan_code(db_session, subscription) == OTHER_PAID_PLAN
    assert await _agents_allowed(db_session, tenant) == OTHER_PAID_AGENTS


async def test_another_workspaces_subscription_is_never_touched(
    db_session: AsyncSession,
) -> None:
    """The tenant scope holds through the new branch as well.

    `CheckoutService` is built with one tenant and its repositories carry the
    predicate, so this is a regression guard rather than a doubt - the branch
    added for F-1 reads a subscription, and a subscription read is exactly the
    kind of thing that acquires a stray `.get(id)` later.
    """
    tenant, subscription, _ = await _workspace(db_session)
    other, other_subscription, other_plans = await _workspace(db_session, slug="other")
    other_subscription.plan_id = other_plans[PAID_PLAN].id
    await db_session.flush()

    _, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)
    await _reverse(db_session, tenant, payment)

    assert await _plan_code(db_session, subscription) == FREE_PLAN
    assert await _plan_code(db_session, other_subscription) == PAID_PLAN
    assert await _agents_allowed(db_session, other) == PAID_AGENTS


async def test_a_cancelled_subscription_is_left_where_the_customer_put_it(
    db_session: AsyncSession,
) -> None:
    """Terminal is terminal in both directions.

    `_apply_purchased_plan` refuses to revive a cancelled subscription when
    money arrives; the withdrawal refuses to rewrite one when money leaves. A
    cancelled workspace already resolves to the default plan through
    `EntitlementService`, so there is nothing to take away either.
    """
    tenant, subscription, plans = await _workspace(db_session)
    _, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)
    subscription.status = SubscriptionStatus.CANCELLED
    subscription.ended_at = NOW
    await db_session.flush()

    assert await _reverse(db_session, tenant, payment) == APPLIED

    await db_session.refresh(subscription)
    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.plan_id == plans[PAID_PLAN].id
    assert await _agents_allowed(db_session, tenant) == FREE_AGENTS


# ------------------------------------------------------------- doing it twice


async def test_the_same_refund_callback_twice_withdraws_once(
    db_session: AsyncSession,
) -> None:
    """Idempotency has to hold for the commercial half too, not just the money.

    The `payment_events` claim stops the second delivery before it decides
    anything, so there is one withdrawal and one audit entry. Without that the
    second pass would find the workspace already on Starter and `change_plan`
    would raise `ConflictError` into a callback handler.
    """
    tenant, subscription, _ = await _workspace(db_session)
    _, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)
    reversal = _transaction(
        reference=str(payment.id),
        transaction=PAID_TRANSACTION,
        amount_cents=9900,
        refunded_cents=9900,
        is_refund=True,
    )

    first = await _apply(db_session, tenant, reversal)
    second = await _apply(db_session, tenant, reversal)

    assert (first, second) == (APPLIED, DUPLICATE)
    await db_session.flush()
    withdrawals = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant.id)
                .where(AuditLog.action == AuditAction.SUBSCRIPTION_PLAN_WITHDRAWN)
            )
        )
        .scalars()
        .all()
    )
    assert len(withdrawals) == 1
    assert await _plan_code(db_session, subscription) == FREE_PLAN


async def test_a_second_partial_reversal_that_empties_the_invoice_withdraws(
    db_session: AsyncSession,
) -> None:
    """Two halves refunded is a full refund, arriving in two notifications.

    Paymob reports a cumulative total, so the branch has to key on what is left
    on the invoice rather than on this notification's own figure. Keying on the
    figure would let anyone keep the plan by asking for their money back twice.
    """
    tenant, subscription, _ = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    await _reverse(db_session, tenant, payment, returned="50.00")
    assert await _plan_code(db_session, subscription) == PAID_PLAN

    await _reverse(
        db_session,
        tenant,
        payment,
        returned="99.00",
        transaction=SECOND_TRANSACTION,
    )

    await db_session.refresh(invoice)
    assert invoice.amount_paid == Decimal("0.00")
    assert await _plan_code(db_session, subscription) == FREE_PLAN


async def test_a_missing_default_plan_does_not_undo_the_refund(
    db_session: AsyncSession,
) -> None:
    """The money is the part that must never be rolled back.

    A deployment whose `DEFAULT_PLAN_CODE` names no catalogue row cannot be
    downgraded to anything. That is a misconfiguration to shout about, not a
    reason to lose the record that a customer was repaid - the same containment
    `_apply_purchased_plan` has in the opposite direction.
    """
    tenant, subscription, plans = await _workspace(db_session)
    invoice, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    body = _transaction(
        reference=str(payment.id),
        transaction=PAID_TRANSACTION,
        amount_cents=9900,
        refunded_cents=9900,
        is_refund=True,
    )
    event = _provider().verify_callback(
        payload=json.dumps({"type": "TRANSACTION", "obj": body}).encode("utf-8"),
        signature=hmac_signature(body, secret=HMAC_SECRET),
    )
    outcome = await CheckoutService(
        db_session,
        tenant_id=tenant.id,
        provider=_provider(),
        default_plan_code="no-such-plan",
    ).apply(event, now=NOW)

    assert outcome == APPLIED
    await db_session.refresh(payment)
    await db_session.refresh(invoice)
    assert payment.status is PaymentStatus.REFUNDED
    assert invoice.amount_paid == Decimal("0.00")
    assert subscription.plan_id == plans[PAID_PLAN].id


async def test_a_reversal_of_an_unknown_reference_changes_no_subscription(
    db_session: AsyncSession,
) -> None:
    """A callback naming nothing must not reach the new branch at all."""
    tenant, subscription, _ = await _workspace(db_session)
    _, payment = await _bought(db_session, tenant, subscription)
    await _settle(db_session, tenant, payment)

    outcome = await _apply(
        db_session,
        tenant,
        _transaction(
            reference=str(uuid.uuid4()),
            transaction="777001",
            amount_cents=9900,
            refunded_cents=9900,
            is_refund=True,
        ),
    )

    assert outcome != APPLIED
    assert await _plan_code(db_session, subscription) == PAID_PLAN
