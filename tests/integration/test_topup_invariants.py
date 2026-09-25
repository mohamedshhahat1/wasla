"""The custom-plan and top-up invariants, swept over a populated ledger (spec 70-72).

One workspace pair is driven through every state the ledger can hold - granted,
pending, paid-but-not-granted, refunded before and after the grant, expired,
platform grants, a custom plan held and another scheduled - using the real
services. Then each invariant is a SQL query that counts violations, and every
count must be zero. The same queries are what `CUSTOM_PLANS_TOPUPS_IMPLEMENTATION.md`
reports against the real Paymob Test database.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.billing import TOPUP_LIMITS, BillingInterval, LimitKey, PlanScope
from app.db.models.enums import PlatformRole
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.user import User
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.custom_plan_offers import PlatformCustomPlanOffers
from app.platform.plan_admin import PlanCatalogAdmin
from app.platform.topup_admin import TopupAdmin
from app.schemas.custom_plan import CustomPlanOfferCreate
from app.schemas.platform_billing import ChangeMode, PlanCreate, SubscriptionChangePlan
from app.schemas.topup import TopupGrantCreate
from app.services.entitlement_service import EntitlementService
from tests.integration.topup_harness import (
    Paymob,
    apply,
    base_now,
    buy,
    callback,
    catalogue,
    later,
    pay,
    product,
    workspace,
)

pytestmark = pytest.mark.integration

AI = TopupEntitlement.PERIOD_AI_TURNS

#: (name, SQL counting violations). Each must return 0.
INVARIANTS: tuple[tuple[str, str], ...] = (
    (
        "T01 a purchase, its invoice and its payment belong to one workspace",
        """SELECT count(*) FROM topup_purchases t
             LEFT JOIN invoices i ON i.id = t.invoice_id
             LEFT JOIN payments p ON p.id = t.payment_id
            WHERE (i.id IS NOT NULL AND i.tenant_id <> t.tenant_id)
               OR (p.id IS NOT NULL AND p.tenant_id <> t.tenant_id)""",
    ),
    (
        "T02 every purchased quantity is positive",
        "SELECT count(*) FROM topup_purchases WHERE quantity <= 0",
    ),
    (
        "T03 a paid top-up invoice is granted, or held with an incident",
        """SELECT count(*) FROM topup_purchases t JOIN invoices i ON i.id = t.invoice_id
            WHERE i.status = 'paid' AND t.status = 'pending'
               OR (t.status = 'paid' AND NOT EXISTS (
                      SELECT 1 FROM billing_incidents b
                       WHERE b.invoice_id = i.id AND b.kind = 'topup_paid_but_not_granted'))""",
    ),
    (
        "T04 nothing unpaid is granted (a purchase grant needs its invoice paid)",
        """SELECT count(*) FROM topup_purchases t JOIN invoices i ON i.id = t.invoice_id
            WHERE t.source = 'purchase' AND t.status = 'granted' AND i.status <> 'paid'""",
    ),
    (
        "T05 a platform grant has no invoice, no payment and no price",
        """SELECT count(*) FROM topup_purchases
            WHERE source = 'platform_grant'
              AND (invoice_id IS NOT NULL OR payment_id IS NOT NULL OR total_amount <> 0)""",
    ),
    (
        "T06 no top-up invoice has an automatic (MIT) payment",
        """SELECT count(*) FROM payments p JOIN invoices i ON i.id = p.invoice_id
            WHERE i.purpose = 'topup' AND p.is_automatic""",
    ),
    (
        "T07 the snapshot is what was invoiced",
        """SELECT count(*) FROM topup_purchases t JOIN invoices i ON i.id = t.invoice_id
            WHERE i.amount_due <> t.total_amount
               OR (i.lines -> 0 ->> 'quantity')::bigint <> t.quantity
               OR (i.lines -> 0 ->> 'entitlement_key') <> t.entitlement_key::text""",
    ),
    (
        "T08 a purchase's product is global or its own workspace's",
        """SELECT count(*) FROM topup_purchases t JOIN topup_products p ON p.id = t.topup_product_id
            WHERE p.tenant_id IS NOT NULL AND p.tenant_id <> t.tenant_id""",
    ),
    (
        "T09 one purchase per invoice",
        """SELECT count(*) FROM (SELECT invoice_id FROM topup_purchases
            WHERE invoice_id IS NOT NULL GROUP BY invoice_id HAVING count(*) > 1) d""",
    ),
    (
        "T10 a granted top-up expires after its grant, at its period end",
        """SELECT count(*) FROM topup_purchases
            WHERE granted_at IS NOT NULL AND (expires_at <= granted_at
                                             OR expires_at <> billing_period_end)""",
    ),
    (
        "C01 a tenant plan has exactly one owner, and only a tenant plan has one",
        """SELECT count(*) FROM plans
            WHERE (scope = 'tenant') <> (tenant_id IS NOT NULL)""",
    ),
    (
        "C02 no subscription holds or awaits another workspace's custom plan",
        """SELECT count(*) FROM subscriptions s
             JOIN plan_versions v ON v.id IN (s.plan_version_id, s.scheduled_plan_version_id)
             JOIN plans p ON p.id = v.plan_id
            WHERE p.tenant_id IS NOT NULL AND p.tenant_id <> s.tenant_id""",
    ),
    (
        "C03 no invoice charges for another workspace's custom plan",
        """SELECT count(*) FROM invoices i
             JOIN plan_versions v ON v.id = i.plan_version_id
             JOIN plans p ON p.id = v.plan_id
            WHERE p.tenant_id IS NOT NULL AND p.tenant_id <> i.tenant_id""",
    ),
    (
        "C04 a subscription's pinned version belongs to its plan",
        """SELECT count(*) FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id
            WHERE v.plan_id <> s.plan_id""",
    ),
    (
        "C05 a custom plan is never public",
        "SELECT count(*) FROM plans WHERE scope = 'tenant' AND is_public",
    ),
    # Custom plan offers (ADR-114).
    (
        "O01 an active offer has a paid invoice for its version",
        """SELECT count(*) FROM custom_plan_offers o
            WHERE o.status = 'active' AND NOT EXISTS (
              SELECT 1 FROM invoices i WHERE i.custom_plan_offer_id = o.id
                 AND i.status = 'paid' AND i.plan_version_id = o.plan_version_id)""",
    ),
    (
        "O02 no paid offer invoice for a declined or cancelled offer",
        """SELECT count(*) FROM invoices i
             JOIN custom_plan_offers o ON o.id = i.custom_plan_offer_id
            WHERE i.status = 'paid' AND o.status IN ('declined', 'cancelled')""",
    ),
    (
        "O03 an offer invoice sells the offered version at its price",
        """SELECT count(*) FROM invoices i
             JOIN custom_plan_offers o ON o.id = i.custom_plan_offer_id
             JOIN plan_versions v ON v.id = o.plan_version_id
            WHERE i.plan_version_id <> o.plan_version_id OR i.amount_due <> v.price""",
    ),
    (
        "O04 no priced custom plan is held without a paid invoice for it",
        """SELECT count(*) FROM subscriptions s JOIN plans p ON p.id = s.plan_id
             JOIN plan_versions v ON v.id = s.plan_version_id
            WHERE p.scope = 'tenant' AND v.price > 0 AND NOT EXISTS (
              SELECT 1 FROM invoices i WHERE i.tenant_id = s.tenant_id AND i.status = 'paid'
                 AND i.plan_version_id IN (SELECT id FROM plan_versions WHERE plan_id = p.id))""",
    ),
    (
        "O05 at most one open offer per workspace",
        """SELECT count(*) FROM (SELECT tenant_id FROM custom_plan_offers
             WHERE status IN ('offered', 'pending_payment')
             GROUP BY tenant_id HAVING count(*) > 1) x""",
    ),
)


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", default_plan_code="starter")


async def test_every_invariant_holds_over_a_populated_ledger(db_session: AsyncSession) -> None:
    now = base_now()
    await catalogue(db_session)
    alpha, alpha_owner, alpha_subscription = await workspace(db_session, now=now, name="Alpha")
    beta, beta_owner, beta_subscription = await workspace(db_session, now=now, name="Beta")
    staff = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    db_session.add(staff)
    await db_session.flush()
    paymob = Paymob()
    admin = TopupAdmin(db_session, settings=_settings())
    transaction = iter(range(930_000_001, 930_001_000))

    # Custom plans: Alpha's offered to Alpha, Beta's (free) held by Beta.
    plans = PlanCatalogAdmin(db_session)
    for tenant, subscription in ((alpha, alpha_subscription), (beta, beta_subscription)):
        read = await plans.create(
            PlanCreate(
                code=f"inv-{uuid.uuid4().hex[:6]}",
                name="Custom",
                price=Decimal("0.00") if tenant is beta else Decimal("500.00"),
                currency="EGP",
                interval=BillingInterval.MONTHLY,
                limits={
                    "agents": 5,
                    "period_messages": 10_000,
                    "period_ai_turns": 7_000,
                    "period_campaign_messages": 1_000,
                    "storage_bytes": 10 * 1024**3,
                    "whatsapp_numbers": 2,
                    "team_members": 10,
                    "knowledge_documents": 500,
                },
                scope=PlanScope.TENANT,
                tenant_id=tenant.id,
                is_public=False,
                reason="Invariant sweep.",
            ),
            actor=staff,
            now=now,
        )
        assert read.current_version is not None
        if tenant is alpha:
            # A priced custom plan reaches its workspace as an offer (ADR-114);
            # the ledger holds it open, unpaid.
            await PlatformCustomPlanOffers(db_session).offer(
                alpha.id,
                CustomPlanOfferCreate(
                    plan_version_id=read.current_version.id, reason="Invariant sweep."
                ),
                actor=staff,
                now=now,
            )
            continue
        await PlatformBillingOperations(db_session, settings=_settings()).change_plan(
            subscription.id,
            SubscriptionChangePlan(
                plan_version_id=read.current_version.id,
                mode=ChangeMode.NOW if tenant is beta else ChangeMode.NEXT_RENEWAL,
                reason="Invariant sweep.",
                expected_revision=subscription.revision,
            ),
            actor=staff,
            now=now,
        )

    # Top-ups in every state, for both workspaces.
    for tenant, owner in ((alpha, alpha_owner), (beta, beta_owner)):
        shared = await product(db_session, entitlement=AI, quantity=1_000)
        own = await product(
            db_session, entitlement=TopupEntitlement.TEAM_MEMBERS, quantity=3, tenant=tenant
        )
        _, granted = await buy(db_session, tenant, owner, paymob, shared, now=now)
        await pay(db_session, tenant.id, paymob, granted, transaction=next(transaction), now=now)
        _, own_paid = await buy(db_session, tenant, owner, paymob, own, now=now)
        await pay(db_session, tenant.id, paymob, own_paid, transaction=next(transaction), now=now)
        await buy(db_session, tenant, owner, paymob, shared, now=now)  # left pending
        _, late = await buy(db_session, tenant, owner, paymob, shared, now=now)
        subscription = alpha_subscription if tenant is alpha else beta_subscription
        after_period = later(subscription.current_period_end, minutes=1)
        await pay(
            db_session, tenant.id, paymob, late, transaction=next(transaction), now=after_period
        )
        _, refunded = await buy(db_session, tenant, owner, paymob, shared, now=now)
        number = next(transaction)
        await pay(db_session, tenant.id, paymob, refunded, transaction=number, now=now)
        await db_session.refresh(refunded)
        await apply(
            db_session,
            tenant.id,
            paymob.provider(),
            callback(refunded, transaction=number, refunded_cents=20_000),
            now=now,
        )
        await db_session.refresh(subscription)
        await admin.grant(
            tenant.id,
            TopupGrantCreate(
                entitlement_key=TopupEntitlement.STORAGE_BYTES,
                quantity=1024**3,
                reason="Invariant sweep grant.",
                expected_subscription_revision=subscription.revision,
            ),
            actor=staff,
            now=now,
        )
    await db_session.flush()

    statuses = set(
        (
            await db_session.scalars(
                select(TopupPurchase.status).where(TopupPurchase.tenant_id.in_([alpha.id, beta.id]))
            )
        ).all()
    )
    assert {
        TopupStatus.GRANTED,
        TopupStatus.PENDING,
        TopupStatus.PAID,
        TopupStatus.REFUND_REVIEW,
    } <= statuses

    for name, sql in INVARIANTS:
        violations = await db_session.scalar(text(sql))
        assert violations == 0, name

    # The cross-feature formula (spec 72), recomputed independently in SQL for
    # every workspace and key and compared with the entitlement engine.
    for tenant in (alpha, beta):
        service = EntitlementService(
            db_session, tenant_id=tenant.id, default_plan_code="starter", clock=lambda: now
        )
        terms = await service.terms()
        assert terms is not None
        for key in LimitKey:
            if key not in TOPUP_LIMITS:
                continue
            rows = await db_session.execute(
                text("""SELECT source::text, coalesce(sum(quantity), 0) FROM topup_purchases
                        WHERE tenant_id = :t AND entitlement_key::text = :k
                          AND status IN ('granted', 'refund_review')
                          AND granted_at <= :now AND expires_at > :now
                        GROUP BY source"""),
                {"t": tenant.id, "k": key.value, "now": now},
            )
            sums = {row[0]: int(row[1]) for row in rows.all()}
            base = terms.limit_for(key)
            expected = (
                None
                if base is None
                else base + sums.get("purchase", 0) + sums.get("platform_grant", 0)
            )
            assert (await service.check(key, additional=0)).limit == expected, (tenant.name, key)
