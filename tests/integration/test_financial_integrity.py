"""PostgreSQL refuses crossed and rewritten money on its own (DB-004, DB-005).

The database audit wrote the ledger with plain SQL and PostgreSQL accepted:
one workspace's payment on another's invoice (X01), a top-up on another's
invoice (X05), an incident on another's payment (X13), an automatic charge on
another's saved card (X14), a subscription pinned to another plan's version
(Y45); and a paid invoice's amounts, version and lines rewritten (Y21-Y23), a
succeeded payment's amount rewritten (Y24), a paid invoice reopened with its
money still on it (Y25), `paid` without `paid_at` (Y26), `succeeded` without
`processed_at` (Y27), a declined offer made active (Y34) and a top-up granted
on an unpaid invoice (Y35).

Each is repeated here and must now be refused, with the SQLSTATE recorded -
23503 for a crossed key, 23514 for a CHECK, 23000 for a history trigger. The
documented reversals, which always give money back, must still succeed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.errors import sqlstate
from app.db.models.billing import BillingInterval, Plan, PlanScope, Subscription, SubscriptionStatus
from app.db.models.enums import PlatformRole
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.payment_method import PaymentMethod
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement
from app.db.models.user import User
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.schemas.custom_plan import CustomPlanOfferCreate
from app.schemas.platform_billing import PlanCreate
from app.services.plan_catalog import PlanCatalog
from tests.billing_fixtures import price_terms
from tests.integration.topup_harness import product

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 9, tzinfo=UTC)
PRICE = Decimal("99.00")
FOREIGN_KEY = "23503"
CHECK = "23514"
INTEGRITY = "23000"
# A tenant plan is only versioned once it names every metered limit.
COMPLETE_LIMITS = {
    "period_messages": 1_000,
    "period_ai_turns": 1_000,
    "period_campaign_messages": 1_000,
    "storage_bytes": 1_000_000,
    "whatsapp_numbers": 1,
    "team_members": 3,
    "knowledge_documents": 10,
}


class Workspace:
    """One tenant's settled ledger: a paid invoice, its payment, a saved card."""

    tenant: Tenant
    plan: Plan
    version_id: uuid.UUID
    subscription: Subscription
    invoice: Invoice
    payment: Payment
    card: PaymentMethod


async def _workspace(session: AsyncSession) -> Workspace:
    built = Workspace()
    tag = uuid.uuid4().hex[:10]
    built.tenant = Tenant(name="Ledger", slug=f"integrity-{tag}")
    built.plan = Plan(
        code=f"integrity-{tag}",
        name="Pro",
        price=PRICE,
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={"agents": 5, "period_ai_turns": 100},
    )
    session.add_all([built.tenant, built.plan])
    await session.flush()
    version = await PlanCatalog(session).current_version(built.plan)
    assert version is not None
    built.version_id = version.id
    built.subscription = Subscription(
        tenant_id=built.tenant.id,
        plan_id=built.plan.id,
        plan_version_id=version.id,
        plan_price_id=(await price_terms(session, version.id)).get("plan_price_id"),
        status=SubscriptionStatus.ACTIVE,
        current_period_start=NOW,
        current_period_end=NOW + timedelta(days=30),
        billing_anchor_at=NOW,
        cancel_at_period_end=False,
    )
    session.add(built.subscription)
    await session.flush()
    built.card = PaymentMethod(
        tenant_id=built.tenant.id,
        provider="paymob",
        provider_token=f"v1:synthetic-{tag}",
        token_fingerprint=f"fp-{tag}",
        masked_pan="xxxx-2346",
        brand="MasterCard",
        is_default=True,
    )
    built.invoice = Invoice(
        tenant_id=built.tenant.id,
        subscription_id=built.subscription.id,
        status=InvoiceStatus.PAID,
        purpose=InvoicePurpose.RENEWAL,
        plan_code=built.plan.code,
        plan_version_id=version.id,
        **(await price_terms(session, version.id)),
        amount_due=PRICE,
        amount_paid=PRICE,
        currency="EGP",
        period_start=NOW,
        period_end=NOW + timedelta(days=30),
        issued_at=NOW,
        paid_at=NOW,
        lines=[{"kind": "subscription", "amount": "99.00"}],
    )
    session.add_all([built.card, built.invoice])
    await session.flush()
    built.payment = Payment(
        tenant_id=built.tenant.id,
        invoice_id=built.invoice.id,
        status=PaymentStatus.SUCCEEDED,
        amount=PRICE,
        currency="EGP",
        provider="paymob",
        provider_reference=f"txn-{tag}",
        refunded_amount=Decimal("0.00"),
        processed_at=NOW,
        applied_at=NOW,
        # An automatic renewal charge on the saved card, settled.
        is_automatic=True,
        collection_state=CollectionState.SETTLED,
        payment_method_id=built.card.id,
    )
    session.add(built.payment)
    await session.flush()
    await _checked(session)
    return built


async def _checked(session: AsyncSession) -> None:
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


Statements = str | tuple[str, ...]


async def _run(session: AsyncSession, statements: Statements, params: dict[str, Any]) -> None:
    """One statement at a time: asyncpg prepares each, and refuses a `;` list."""
    for statement in (statements,) if isinstance(statements, str) else statements:
        await session.execute(text(statement), params)


async def _refused(session: AsyncSession, statements: Statements, params: dict[str, Any]) -> str:
    """Run `statements`, firing the commit-time checks, and return the refusal."""
    with pytest.raises(DBAPIError) as refused:
        async with session.begin_nested():
            await _run(session, statements, params)
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    return sqlstate(refused.value) or ""


async def _accepted(session: AsyncSession, statements: Statements, params: dict[str, Any]) -> None:
    async with session.begin_nested():
        await _run(session, statements, params)
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


# ------------------------------------------------------------------ DB-004


_PAYMENT = (
    "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency, provider,"
    " refunded_amount, is_automatic, payment_method_id, collection_state, revision)"
    " VALUES (:id, :tenant, :invoice, 'pending', 99.00, 'EGP', 'paymob', 0, :automatic,"
    " :card, :state, 1)"
)


async def test_a_payment_cannot_collect_another_workspaces_invoice(
    db_session: AsyncSession,
) -> None:
    """X01."""
    a, b = await _workspace(db_session), await _workspace(db_session)
    params = {
        "id": uuid.uuid4(),
        "tenant": a.tenant.id,
        "invoice": b.invoice.id,
        "automatic": False,
        "card": None,
        "state": None,
    }
    assert await _refused(db_session, _PAYMENT, params) == FOREIGN_KEY


async def test_an_automatic_charge_cannot_use_another_workspaces_card(
    db_session: AsyncSession,
) -> None:
    """X14."""
    a, b = await _workspace(db_session), await _workspace(db_session)
    params = {
        "id": uuid.uuid4(),
        "tenant": a.tenant.id,
        "invoice": a.invoice.id,
        "automatic": True,
        "card": b.card.id,
        "state": "claimed",
    }
    assert await _refused(db_session, _PAYMENT, params) == FOREIGN_KEY


async def test_an_incident_cannot_name_another_workspaces_payment(
    db_session: AsyncSession,
) -> None:
    """X13, and a workspace-less incident naming money at all."""
    a, b = await _workspace(db_session), await _workspace(db_session)
    statement = (
        "INSERT INTO billing_incidents (id, tenant_id, kind, status, dedupe_key, payment_id,"
        " created_at, updated_at) VALUES (:id, :tenant, 'duplicate_payment', 'open', :key,"
        " :payment, now(), now())"
    )
    crossed = {"id": uuid.uuid4(), "tenant": a.tenant.id, "key": "x13", "payment": b.payment.id}
    assert await _refused(db_session, statement, crossed) == FOREIGN_KEY
    anonymous = {"id": uuid.uuid4(), "tenant": None, "key": "x13n", "payment": b.payment.id}
    assert await _refused(db_session, statement, anonymous) == CHECK


async def test_a_topup_cannot_be_paid_by_another_workspaces_invoice(
    db_session: AsyncSession,
) -> None:
    """X05."""
    a, b = await _workspace(db_session), await _workspace(db_session)
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=5)
    statement = (
        "INSERT INTO topup_purchases (id, tenant_id, topup_product_id, source, product_code,"
        " product_name, entitlement_key, quantity, unit_price, total_amount, currency,"
        " billing_period_start, billing_period_end, expires_at, status, invoice_id, revision,"
        " created_at, updated_at) VALUES (:id, :tenant, :product, 'purchase', 'x', 'x',"
        " 'period_ai_turns', 5, 99.00, 99.00, 'EGP', :start, :end, :end, 'pending', :invoice,"
        " 1, now(), now())"
    )
    params = {
        "id": uuid.uuid4(),
        "tenant": a.tenant.id,
        "product": item.id,
        "start": NOW,
        "end": NOW + timedelta(days=30),
        "invoice": b.invoice.id,
    }
    assert await _refused(db_session, statement, params) == FOREIGN_KEY


async def test_a_subscription_is_pinned_only_to_its_own_plans_version(
    db_session: AsyncSession,
) -> None:
    """Y45, and an invoice of another workspace's subscription."""
    a, b = await _workspace(db_session), await _workspace(db_session)
    assert (
        await _refused(
            db_session,
            "UPDATE subscriptions SET plan_version_id = :version WHERE id = :id",
            {"version": b.version_id, "id": a.subscription.id},
        )
        == FOREIGN_KEY
    )
    assert await _refused(
        db_session,
        "UPDATE invoices SET subscription_id = :sub WHERE id = :id",
        {"sub": b.subscription.id, "id": a.invoice.id},
    ) in (FOREIGN_KEY, INTEGRITY)


# ------------------------------------------------------------------ DB-005


@pytest.mark.parametrize(
    "change",
    [
        "amount_due = 1.00",  # Y21
        "plan_version_id = NULL",  # Y22
        "lines = '[]'::jsonb",  # Y23
        "period_end = period_end + interval '1 day'",
        "currency = 'EGP', plan_code = 'another'",
        "status = 'open', paid_at = NULL",  # Y25: reopened, money still on it
        "paid_at = paid_at - interval '1 day'",
    ],
)
async def test_a_paid_invoice_keeps_its_terms(db_session: AsyncSession, change: str) -> None:
    a = await _workspace(db_session)
    assert (
        await _refused(
            db_session,
            f"UPDATE invoices SET {change} WHERE id = :id",  # noqa: S608
            {"id": a.invoice.id},
        )
        == INTEGRITY
    )


async def test_settled_states_carry_their_moment(db_session: AsyncSession) -> None:
    """Y26 and Y27."""
    a = await _workspace(db_session)
    assert await _refused(
        db_session,
        "UPDATE invoices SET paid_at = NULL WHERE id = :id",
        {"id": a.invoice.id},
    ) in (CHECK, INTEGRITY)
    open_invoice = uuid.uuid4()
    await _accepted(
        db_session,
        "INSERT INTO invoices (id, tenant_id, status, purpose, plan_code, amount_due,"
        " amount_paid, currency, period_start, period_end, lines, collection_attempts, revision)"
        " VALUES (:id, :tenant, 'open', 'checkout', 'pro', 99.00, 0, 'EGP', :start, :end, '[]',"
        " 0, 1)",
        {"id": open_invoice, "tenant": a.tenant.id, "start": NOW, "end": NOW + timedelta(1)},
    )
    assert (
        await _refused(
            db_session,
            "UPDATE invoices SET status = 'paid' WHERE id = :id",
            {"id": open_invoice},
        )
        == CHECK
    )
    assert await _refused(
        db_session,
        (
            "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency,"
            " provider, refunded_amount, is_automatic, revision) VALUES (:id, :tenant,"
            " :invoice, 'failed', 99.00, 'EGP', 'paymob', 0, false, 1)",
            "UPDATE payments SET status = 'succeeded' WHERE id = :id",
        ),
        {"id": uuid.uuid4(), "tenant": a.tenant.id, "invoice": open_invoice},
    ) in (CHECK, INTEGRITY)


@pytest.mark.parametrize(
    "change",
    [
        "amount = 1.00",  # Y24
        "provider_reference = 'another-transaction'",
        "tenant_id = tenant_id, processed_at = processed_at + interval '1 hour'",
        "status = 'pending'",
        "applied_at = NULL",
    ],
)
async def test_collected_money_keeps_what_it_was(db_session: AsyncSession, change: str) -> None:
    a = await _workspace(db_session)
    assert (
        await _refused(
            db_session,
            f"UPDATE payments SET {change} WHERE id = :id",  # noqa: S608
            {"id": a.payment.id},
        )
        == INTEGRITY
    )


async def test_a_topup_is_not_granted_on_an_unpaid_invoice(db_session: AsyncSession) -> None:
    """Y35: checked when the purchase becomes granted, at commit."""
    a = await _workspace(db_session)
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=5)
    invoice, purchase = uuid.uuid4(), uuid.uuid4()
    await _accepted(
        db_session,
        "INSERT INTO invoices (id, tenant_id, status, purpose, plan_code, amount_due,"
        " amount_paid, currency, period_start, period_end, lines, collection_attempts, revision)"
        " VALUES (:id, :tenant, 'open', 'topup', 'topup', 99.00, 0, 'EGP', :start, :end, '[]',"
        " 0, 1)",
        {"id": invoice, "tenant": a.tenant.id, "start": NOW, "end": NOW + timedelta(days=30)},
    )
    await _accepted(
        db_session,
        "INSERT INTO topup_purchases (id, tenant_id, topup_product_id, source, product_code,"
        " product_name, entitlement_key, quantity, unit_price, total_amount, currency,"
        " billing_period_start, billing_period_end, expires_at, status, invoice_id, revision,"
        " created_at, updated_at) VALUES (:id, :tenant, :product, 'purchase', 'x', 'x',"
        " 'period_ai_turns', 5, 99.00, 99.00, 'EGP', :start, :end, :end, 'pending', :invoice,"
        " 1, now(), now())",
        {
            "id": purchase,
            "tenant": a.tenant.id,
            "product": item.id,
            "start": NOW,
            "end": NOW + timedelta(days=30),
            "invoice": invoice,
        },
    )
    assert (
        await _refused(
            db_session,
            "UPDATE topup_purchases SET status = 'granted', granted_at = now() WHERE id = :id",
            {"id": purchase},
        )
        == INTEGRITY
    )


async def test_a_declined_offer_cannot_be_made_active(db_session: AsyncSession) -> None:
    """Y34: an offer becomes active only with a paid invoice of its own."""
    a = await _workspace(db_session)
    staff = User(
        email=f"integrity-staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    db_session.add(staff)
    await db_session.flush()
    # The catalogue reads versions against the real clock.
    now = datetime.now(UTC)
    created = await PlanCatalogAdmin(db_session).create(
        PlanCreate(
            code=f"integrity-abc-{uuid.uuid4().hex[:6]}",
            name="ABC",
            price=PRICE,
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"agents": 9, **COMPLETE_LIMITS},
            scope=PlanScope.TENANT,
            tenant_id=a.tenant.id,
            is_public=False,
            reason="Deal.",
        ),
        actor=staff,
        now=now,
    )
    assert created.current_version is not None
    offer = await PlatformCustomPlanOffers(db_session).offer(
        a.tenant.id,
        CustomPlanOfferCreate(plan_version_id=created.current_version.id, reason="Deal."),
        actor=staff,
        now=now,
    )
    await db_session.execute(
        text(
            "UPDATE custom_plan_offers SET status = 'declined', declined_at = now() WHERE id = :id"
        ),
        {"id": offer.id},
    )
    assert (
        await _refused(
            db_session,
            (
                "UPDATE custom_plan_offers SET status = 'active', activated_at = now(),"
                " accepted_at = now() WHERE id = :id"
            ),
            {"id": offer.id},
        )
        == INTEGRITY
    )


# ------------------------------------------------- what must still work


async def test_a_refund_reopens_a_paid_invoice_because_money_went_back(
    db_session: AsyncSession,
) -> None:
    a = await _workspace(db_session)
    await _accepted(
        db_session,
        (
            "UPDATE payments SET refunded_amount = 99.00, status = 'refunded',"
            " refunded_at = now() WHERE id = :payment",
            "UPDATE invoices SET amount_paid = 0, status = 'open', paid_at = NULL"
            " WHERE id = :invoice",
        ),
        {"payment": a.payment.id, "invoice": a.invoice.id},
    )


async def test_a_goodwill_refund_leaves_the_invoice_paid(db_session: AsyncSession) -> None:
    a = await _workspace(db_session)
    await _accepted(
        db_session,
        (
            "UPDATE payments SET refunded_amount = 20.00, refunded_at = now() WHERE id = :payment",
            "UPDATE invoices SET amount_paid = 79.00 WHERE id = :invoice",
        ),
        {"payment": a.payment.id, "invoice": a.invoice.id},
    )


async def test_an_operators_full_refund_voids_the_invoice(db_session: AsyncSession) -> None:
    a = await _workspace(db_session)
    await _accepted(
        db_session,
        (
            "UPDATE payments SET refunded_amount = 99.00, status = 'refunded',"
            " refunded_at = now() WHERE id = :payment",
            "UPDATE invoices SET amount_paid = 0, status = 'void', paid_at = NULL,"
            " voided_at = now(), notes = 'Refunded in full.' WHERE id = :invoice",
        ),
        {"payment": a.payment.id, "invoice": a.invoice.id},
    )
    assert (
        await _refused(
            db_session,
            "UPDATE invoices SET status = 'open' WHERE id = :invoice",
            {"invoice": a.invoice.id},
        )
        == INTEGRITY
    )


async def test_a_removed_card_detaches_from_the_money_it_collected(
    db_session: AsyncSession,
) -> None:
    """SET NULL of the card column only - the tenant on the payment stays."""
    a = await _workspace(db_session)
    await _accepted(db_session, "DELETE FROM payment_methods WHERE id = :id", {"id": a.card.id})
    row = (
        await db_session.execute(
            text("SELECT tenant_id, payment_method_id FROM payments WHERE id = :id"),
            {"id": a.payment.id},
        )
    ).one()
    assert row == (a.tenant.id, None)
