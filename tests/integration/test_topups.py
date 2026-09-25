"""Top-ups end to end: bought, paid, granted once, expired, refunded (ADR-113).

Every test drives the real services - `TopupService`, `CheckoutService`, the
real Paymob adapter's signature verification, `InvoiceSettlement`,
`TopupLedger`, `EntitlementService` and the billing sweep - against PostgreSQL.
Paymob is faked at the socket only (`topup_harness.Paymob`).

The properties, each named where it is proved:

* the effective limit is plan + active paid top-ups + active platform grants,
  and a top-up never touches the plan, its version or its price;
* a paid top-up is granted exactly once, and nothing unpaid is ever granted;
* what was shown at checkout is what is granted, whatever the product becomes;
* a top-up expires with its period, with no carry-over, and expiry of a
  capacity deletes nothing;
* a platform grant is never a sale; a top-up invoice is never collected from a
  saved card;
* refunds never subtract on their own.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, PlanLimitExceededError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import LimitKey, SubscriptionStatus
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.enums import PlatformRole
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.platform.topup_admin import TopupAdmin
from app.repositories.invoice_repository import PlatformInvoiceRepository
from app.schemas.topup import TopupGrantCreate, TopupProductUpdate, TopupRefundReview
from app.services.checkout_service import APPLIED, DECLINED, DUPLICATE, CheckoutService
from app.services.entitlement_service import EntitlementService
from app.services.invoice_service import InvoiceService
from app.services.plan_catalog import PlanCatalog
from app.services.recurring_service import MAX_COLLECTION_ATTEMPTS
from app.services.subscription_service import SubscriptionService
from app.services.topup_service import TopupService
from app.workers import billing_worker as worker_module
from app.workers.billing_worker import BillingWorker
from tests.fakes import as_database
from tests.integration.plan_catalogue import own_plan
from tests.integration.topup_harness import (
    GIB,
    Paymob,
    apply,
    base_now,
    buy,
    callback,
    catalogue,
    held,
    later,
    pay,
    product,
    standing,
    workspace,
)

pytestmark = pytest.mark.integration

AI = TopupEntitlement.PERIOD_AI_TURNS


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", default_plan_code="starter")


async def _purchase(session: AsyncSession, purchase_id: uuid.UUID) -> TopupPurchase:
    await session.flush()
    row = await session.get(TopupPurchase, purchase_id)
    assert row is not None
    await session.refresh(row)
    return row


async def _staff(session: AsyncSession) -> User:
    user = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    session.add(user)
    await session.flush()
    return user


# ------------------------------------------------------------ the core path


async def test_a_paid_topup_raises_the_limit_once_and_leaves_the_plan_alone(
    db_session: AsyncSession,
) -> None:
    """Spec 78-80: base 5,000 AI turns, +10,000 bought, 15,000 after payment.

    Replaying the genuine callback leaves 15,000 (not 25,000), and the plan,
    its version and its recurring price are exactly what they were.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    plan_before = (subscription.plan_id, subscription.plan_version_id)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000, price="200.00")

    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 5_000
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None
    assert invoice.purpose is InvoicePurpose.TOPUP
    assert invoice.plan_version_id is None
    assert invoice.amount_due == Decimal("200.00")
    assert len(paymob.intentions()) == 1

    signed = callback(payment, transaction=900_000_001)
    assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == APPLIED

    await db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.GRANTED
    assert purchase.granted_at == now
    assert purchase.expires_at == subscription.current_period_end
    entitlement = await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)
    assert (entitlement.base_limit, entitlement.topup_limit, entitlement.grant_limit) == (
        5_000,
        10_000,
        0,
    )
    assert entitlement.limit == 15_000

    for _ in range(4):
        assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == DUPLICATE
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 15_000
    granted = await db_session.scalar(
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id == tenant.id)
        .where(TopupPurchase.status == TopupStatus.GRANTED)
    )
    assert granted == 1

    await db_session.refresh(subscription)
    assert (subscription.plan_id, subscription.plan_version_id) == plan_before
    renewal_price = await EntitlementService(db_session, tenant_id=tenant.id).terms()
    assert renewal_price is not None and renewal_price.price == Decimal("99.00")


async def test_a_declined_topup_payment_grants_nothing(db_session: AsyncSession) -> None:
    """TU-10: a failed payment leaves the allowance exactly where it was."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)

    declined = callback(payment, transaction=900_000_101, success=False)
    assert await apply(db_session, tenant.id, paymob.provider(), declined, now=now) == DECLINED

    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.PENDING
    assert purchase.granted_at is None
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 5_000


async def test_the_topup_granted_is_the_one_shown_at_checkout(db_session: AsyncSession) -> None:
    """Spec 20/58, TU-04/TU-15: repricing, resizing or retiring a product after
    its page opened changes nothing about what that page sells or grants.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    paymob = Paymob()
    staff = await _staff(db_session)
    item = await product(db_session, entitlement=AI, quantity=10_000, price="200.00")
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)

    admin = TopupAdmin(db_session, settings=_settings())
    await admin.update_product(
        item.id,
        TopupProductUpdate(
            quantity=1, price=Decimal("999.00"), expected_revision=item.revision, reason="reprice"
        ),
        actor=staff,
    )
    await db_session.refresh(item)
    await admin.set_active(
        item.id, active=False, expected_revision=item.revision, reason="retire", actor=staff
    )

    assert payment.amount == Decimal("200.00")
    assert (
        await apply(
            db_session,
            tenant.id,
            paymob.provider(),
            callback(payment, transaction=900_000_201),
            now=now,
        )
        == APPLIED
    )
    purchase = await _purchase(db_session, started.purchase_id)
    assert (purchase.quantity, purchase.unit_price, purchase.status) == (
        10_000,
        Decimal("200.00"),
        TopupStatus.GRANTED,
    )
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 15_000

    # And the ledger itself refuses to be rewritten, whoever tries.
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE topup_purchases SET quantity = 1 WHERE id = :id"),
                {"id": purchase.id},
            )


# ------------------------------------------------------------------ expiry


async def test_a_usage_topup_ends_with_its_period_and_does_not_carry_over(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 28/81, TU-05/TU-09: active up to the period end, excluded from it on.

    The billing sweep then records the expiry; the purchase's own history -
    quantity, grant time, expiry - is not rewritten.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_301),
        now=now,
    )
    end = subscription.current_period_end

    assert (
        await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=later(end, seconds=-1))
    ).limit == 15_000
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=end)).limit == 5_000

    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: None)
    worker = BillingWorker(database=as_database(_Handle(db_session)), settings=_settings())
    await worker.run_once(now=later(end, minutes=1))

    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.EXPIRED
    assert (purchase.quantity, purchase.granted_at, purchase.expires_at) == (10_000, now, end)
    expired = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_TOPUP_EXPIRED)
        .where(AuditLog.target_id == purchase.id)
    )
    assert expired == 1
    # The period rolled over and the base allowance is back to the plan's own.
    await db_session.refresh(subscription)
    assert subscription.current_period_start == end
    assert (
        await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=later(end, minutes=2))
    ).limit == 5_000


async def test_a_capacity_topup_expiry_keeps_every_number_and_blocks_new_ones(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 29/31/82, TU-12: base 1 + 2 = 3 numbers; the fourth is refused.

    When the top-up expires - by the clock, and then recorded by the real
    billing sweep - the three stay connected, the workspace reads `over_limit`
    with nothing remaining, and a new number is refused.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(
        db_session, entitlement=TopupEntitlement.WHATSAPP_NUMBERS, quantity=2, price="150.00"
    )
    _, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_401),
        now=now,
    )

    guard = EntitlementService(db_session, tenant_id=tenant.id, clock=lambda: now)
    for index in range(3):
        await guard.reserve_or_refuse(LimitKey.WHATSAPP_NUMBERS)
        db_session.add(_number(tenant.id, index))
        await db_session.flush()
    with pytest.raises(PlanLimitExceededError):
        await guard.reserve_or_refuse(LimitKey.WHATSAPP_NUMBERS)

    after = later(subscription.current_period_end, seconds=1)
    expired = EntitlementService(db_session, tenant_id=tenant.id, clock=lambda: after)
    state = await expired.check(LimitKey.WHATSAPP_NUMBERS, additional=0)
    assert (state.limit, state.used, state.remaining, state.over_limit) == (1, 3, 0, True)
    with pytest.raises(PlanLimitExceededError):
        await expired.reserve_or_refuse(LimitKey.WHATSAPP_NUMBERS)
    assert await held(db_session, WhatsAppAccount, tenant.id) == 3

    # The sweep records the expiry and rolls the period; it deletes nothing.
    monkeypatch.setattr(worker_module, "build_checkout_provider", lambda settings: None)
    worker = BillingWorker(database=as_database(_Handle(db_session)), settings=_settings())
    await worker.run_once(now=later(subscription.current_period_end, minutes=1))
    expired_rows = await db_session.scalar(
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id == tenant.id)
        .where(TopupPurchase.status == TopupStatus.EXPIRED)
    )
    assert expired_rows == 1
    assert await held(db_session, WhatsAppAccount, tenant.id) == 3


def _number(tenant_id: uuid.UUID, index: int) -> WhatsAppAccount:
    return WhatsAppAccount(
        tenant_id=tenant_id,
        phone_number_id=f"tu-{uuid.uuid4().hex[:12]}",
        waba_id=f"waba-{index}",
        display_phone_number=f"+2010000001{index}",
    )


class _Handle:
    """Hands the worker the suite's transaction-scoped session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


# ------------------------------------------------------------------ grants


async def test_a_platform_grant_adds_allowance_and_is_never_a_sale(
    db_session: AsyncSession,
) -> None:
    """Spec 40-42, TU-07: a grant has no invoice, no payment and no price.

    It shows as `platform_grant_limit`, beside a paid top-up's `topup_limit`,
    and the database refuses a grant that pretends otherwise.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    payments_before = await db_session.scalar(select(func.count()).select_from(Payment))
    invoices_before = await db_session.scalar(select(func.count()).select_from(Invoice))

    read = await TopupAdmin(db_session, settings=_settings()).grant(
        tenant.id,
        TopupGrantCreate(
            entitlement_key=TopupEntitlement.PERIOD_MESSAGES,
            quantity=5_000,
            reason="Compensation for an outage.",
            expected_subscription_revision=subscription.revision,
        ),
        actor=staff,
        now=now,
    )
    assert read.source is TopupSource.PLATFORM_GRANT
    assert (read.invoice_id, read.payment_id, read.amount) == (None, None, "0.00")
    assert await db_session.scalar(select(func.count()).select_from(Payment)) == payments_before
    assert await db_session.scalar(select(func.count()).select_from(Invoice)) == invoices_before

    state = await standing(db_session, tenant, LimitKey.PERIOD_MESSAGES, at=now)
    assert (state.base_limit, state.topup_limit, state.grant_limit, state.limit) == (
        10_000,
        0,
        5_000,
        15_000,
    )
    audit = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == AuditAction.BILLING_TOPUP_PLATFORM_GRANTED)
        .where(AuditLog.target_id == read.id)
    )
    assert audit is not None and audit.meta is not None
    assert audit.meta["reason"] == "Compensation for an outage."
    assert audit.meta["before"]["effective_limit"] == 10_000
    assert audit.meta["after"]["effective_limit"] == 15_000
    assert audit.meta["actor_role"] == "platform_admin"

    # A grant carrying a payment is refused by the table itself.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE topup_purchases SET total_amount = 1 WHERE id = :id"),
                {"id": read.id},
            )


async def test_a_stale_subscription_revision_refuses_a_grant(db_session: AsyncSession) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    with pytest.raises(ConflictError):
        await TopupAdmin(db_session, settings=_settings()).grant(
            tenant.id,
            TopupGrantCreate(
                entitlement_key=TopupEntitlement.TEAM_MEMBERS,
                quantity=1,
                reason="stale view",
                expected_subscription_revision=subscription.revision + 7,
            ),
            actor=staff,
            now=now,
        )


async def test_the_effective_limit_is_plan_plus_topups_plus_grants_for_every_key(
    db_session: AsyncSession,
) -> None:
    """Spec 3/72: the formula, with populated data, for all seven keys."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    admin = TopupAdmin(db_session, settings=_settings())
    bought = {key: 11 * (index + 1) for index, key in enumerate(TopupEntitlement)}
    given = {key: 3 * (index + 1) for index, key in enumerate(TopupEntitlement)}
    for key in TopupEntitlement:
        item = await product(db_session, entitlement=key, quantity=bought[key])
        _, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
        await apply(
            db_session,
            tenant.id,
            paymob.provider(),
            callback(payment, transaction=901_000_000 + len(paymob.intentions())),
            now=now,
        )
        await db_session.refresh(subscription)
        await admin.grant(
            tenant.id,
            TopupGrantCreate(
                entitlement_key=key,
                quantity=given[key],
                reason="formula",
                expected_subscription_revision=subscription.revision,
            ),
            actor=staff,
            now=now,
        )

    terms = await EntitlementService(db_session, tenant_id=tenant.id).terms()
    assert terms is not None
    for key in TopupEntitlement:
        state = await standing(db_session, tenant, key.limit_key, at=now)
        base = terms.limit_for(key.limit_key)
        assert base is not None
        assert (state.base_limit, state.topup_limit, state.grant_limit) == (
            base,
            bought[key],
            given[key],
        ), key
        assert state.limit == base + bought[key] + given[key], key
    # Storage in bytes, never floating gigabytes.
    storage = await standing(db_session, tenant, LimitKey.STORAGE_BYTES, at=now)
    assert storage.base_limit == 10 * GIB


# --------------------------------------------------- never a saved-card charge


async def test_a_topup_invoice_is_never_claimed_for_a_saved_card_charge(
    db_session: AsyncSession,
) -> None:
    """Spec 25, TU-03: even a top-up invoice that looks like a renewal in every
    other way - issued, for the current period, at the version's price - is
    not collectible, and the ledger refuses an automatic payment against one.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, _, subscription = await workspace(db_session, now=now)
    terms = await EntitlementService(db_session, tenant_id=tenant.id).terms()
    assert terms is not None
    disguised = Invoice(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.TOPUP,
        plan_code="topup:disguised",
        plan_version_id=terms.id,
        amount_due=terms.price,
        amount_paid=Decimal("0.00"),
        currency="EGP",
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
        issued_at=now,
        lines=[],
    )
    db_session.add(disguised)
    await db_session.flush()

    claimed = await PlatformInvoiceRepository(db_session).claim_collectible(
        before=later(now, days=40), max_attempts=MAX_COLLECTION_ATTEMPTS
    )
    assert disguised.id not in {invoice.id for invoice in claimed}

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Payment(
                    tenant_id=tenant.id,
                    invoice_id=disguised.id,
                    status=PaymentStatus.PENDING,
                    amount=terms.price,
                    currency="EGP",
                    provider="paymob",
                    is_automatic=True,
                    collection_state=CollectionState.CLAIMED,
                    refunded_amount=Decimal("0.00"),
                )
            )
            await db_session.flush()


# ------------------------------------------------ paid but not grantable


async def test_money_arriving_after_the_period_is_held_and_raised(
    db_session: AsyncSession,
) -> None:
    """A page paid after its period ended grants nothing it can no longer mean:
    the money is recorded, the purchase is `paid`, and an incident is raised.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    late = later(subscription.current_period_end, minutes=5)

    assert (
        await apply(
            db_session,
            tenant.id,
            paymob.provider(),
            callback(payment, transaction=900_000_501),
            now=late,
        )
        == APPLIED
    )
    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.PAID
    assert purchase.granted_at is None
    incident = await db_session.scalar(
        select(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
        .where(BillingIncident.kind == BillingIncidentKind.TOPUP_PAID_BUT_NOT_GRANTED)
    )
    assert incident is not None
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).topup_limit == 0


# ------------------------------------------------------------------ refunds


async def test_a_refund_before_the_grant_cancels_and_grants_nothing(
    db_session: AsyncSession,
) -> None:
    """Spec 44: paid, not granted, refunded in full - the grant stays zero."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000, price="200.00")
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    late = later(subscription.current_period_end, minutes=5)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_601),
        now=late,
    )
    await db_session.refresh(payment)

    refund = callback(payment, transaction=900_000_601, refunded_cents=20_000)
    assert await apply(db_session, tenant.id, paymob.provider(), refund, now=late) == APPLIED

    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.CANCELLED
    assert purchase.granted_at is None


async def test_a_refund_after_the_grant_never_subtracts_on_its_own(
    db_session: AsyncSession,
) -> None:
    """Spec 45, TU-12: the allowance stays until an operator decides; deciding
    to withdraw removes it from the limit and deletes nothing.

    The workspace has used more than its plan's own allowance of numbers, so
    the incident is `topup_refund_after_consumption`.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    item = await product(
        db_session, entitlement=TopupEntitlement.WHATSAPP_NUMBERS, quantity=2, price="150.00"
    )
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_701),
        now=now,
    )
    for index in range(3):
        db_session.add(_number(tenant.id, index))
    await db_session.flush()
    await db_session.refresh(payment)

    refund = callback(payment, transaction=900_000_701, refunded_cents=15_000)
    assert await apply(db_session, tenant.id, paymob.provider(), refund, now=now) == APPLIED

    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.REFUND_REVIEW
    assert (await standing(db_session, tenant, LimitKey.WHATSAPP_NUMBERS, at=now)).limit == 3
    incident = await db_session.scalar(
        select(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
        .where(BillingIncident.kind == BillingIncidentKind.TOPUP_REFUND_AFTER_CONSUMPTION)
    )
    assert incident is not None
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None and invoice.issued_at is None, "a top-up is never dunned"

    read = await TopupAdmin(db_session, settings=_settings()).review_refund(
        purchase.id,
        TopupRefundReview(
            decision="withdraw", reason="refunded", expected_revision=purchase.revision
        ),
        actor=staff,
        now=now,
    )
    assert read.status is TopupStatus.CANCELLED
    state = await standing(db_session, tenant, LimitKey.WHATSAPP_NUMBERS, at=now)
    assert (state.limit, state.used, state.over_limit, state.remaining) == (1, 3, True, 0)
    held = await db_session.scalar(
        select(func.count())
        .select_from(WhatsAppAccount)
        .where(WhatsAppAccount.tenant_id == tenant.id)
    )
    assert held == 3


async def test_an_unconsumed_granted_refund_is_reversal_blocked_and_can_be_kept(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000, price="200.00")
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_801),
        now=now,
    )
    await db_session.refresh(payment)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_801, refunded_cents=20_000),
        now=now,
    )
    incident = await db_session.scalar(
        select(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
        .where(BillingIncident.kind == BillingIncidentKind.TOPUP_ENTITLEMENT_REVERSAL_BLOCKED)
    )
    assert incident is not None
    purchase = await _purchase(db_session, started.purchase_id)
    read = await TopupAdmin(db_session, settings=_settings()).review_refund(
        purchase.id,
        TopupRefundReview(decision="keep", reason="goodwill", expected_revision=purchase.revision),
        actor=staff,
        now=now,
    )
    assert read.status is TopupStatus.GRANTED
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 15_000


async def test_voiding_an_abandoned_topup_invoice_cancels_its_purchase(
    db_session: AsyncSession,
) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, _ = await buy(db_session, tenant, owner, Paymob(), item, now=now)
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None
    await InvoiceService(db_session, tenant_id=tenant.id).void(
        invoice.id, reason="abandoned", expected_revision=invoice.revision
    )
    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.status is TopupStatus.CANCELLED


# ----------------------------------------------------- idempotency, refusal


async def test_the_same_idempotency_key_is_the_same_purchase(db_session: AsyncSession) -> None:
    """Spec 57, TU-11: one purchase, one invoice, one Paymob intention."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, _ = await buy(db_session, tenant, owner, paymob, item, now=now, idempotency_key="k-1")

    with pytest.raises(ConflictError) as refused:
        await buy(db_session, tenant, owner, paymob, item, now=now, idempotency_key="k-1")
    assert refused.value.details == {"purchase_id": str(started.purchase_id)}
    assert len(paymob.intentions()) == 1
    purchases = await db_session.scalar(
        select(func.count()).select_from(TopupPurchase).where(TopupPurchase.tenant_id == tenant.id)
    )
    assert purchases == 1


async def test_an_unlimited_plan_key_cannot_be_topped_up(db_session: AsyncSession) -> None:
    """Spec 73: a top-up cannot make unlimited more unlimited, so it is not sold."""
    now = base_now()
    await catalogue(db_session)
    await own_plan(db_session, code="unlimited", price=Decimal("499.00"), limits={"agents": 3})
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="unlimited")
    item = await product(db_session, entitlement=AI, quantity=10_000)
    with pytest.raises(ConflictError):
        await buy(db_session, tenant, owner, Paymob(), item, now=now)
    state = await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)
    assert state.limit is None and state.remaining is None


async def test_a_retired_product_sells_no_more_and_changes_no_holder(
    db_session: AsyncSession,
) -> None:
    """Spec 59: deactivation stops new purchases and nothing else."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    _, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_000_901),
        now=now,
    )
    item.is_active = False
    await db_session.flush()
    with pytest.raises(NotFoundError):
        await buy(db_session, tenant, owner, paymob, item, now=now)
    assert (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit == 15_000


async def test_a_bought_product_cannot_be_hard_deleted(db_session: AsyncSession) -> None:
    """Spec 60: purchases are financial history; delete is refused (409)."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    item = await product(db_session, entitlement=AI, quantity=10_000)
    await buy(db_session, tenant, owner, Paymob(), item, now=now)
    admin = TopupAdmin(db_session, settings=_settings())
    with pytest.raises(ConflictError):
        await admin.delete_product(item.id, reason="tidy", actor=staff)

    unused = await product(db_session, entitlement=AI, quantity=5)
    await admin.delete_product(unused.id, reason="never sold", actor=staff)
    assert await db_session.get(type(unused), unused.id) is None


# ------------------------------------------------------------ isolation


async def test_one_workspace_cannot_see_buy_or_read_anothers_topups(
    db_session: AsyncSession,
) -> None:
    """Spec 69, TU-06: another workspace's product is invisible and unbuyable,
    its purchase unreadable, and the table refuses a cross-tenant purchase.
    """
    now = base_now()
    await catalogue(db_session)
    alpha, alpha_owner, _ = await workspace(db_session, now=now, name="Alpha")
    beta, beta_owner, _ = await workspace(db_session, now=now, name="Beta")
    paymob = Paymob()
    alphas = await product(
        db_session, entitlement=AI, quantity=50_000, price="600.00", tenant=alpha
    )
    shared = await product(db_session, entitlement=AI, quantity=10_000)

    beta_catalogue = await TopupService(db_session, tenant_id=beta.id).catalogue()
    assert shared.id in {row.id for row in beta_catalogue}
    assert alphas.id not in {row.id for row in beta_catalogue}
    alpha_catalogue = await TopupService(db_session, tenant_id=alpha.id).catalogue()
    assert alphas.id in {row.id for row in alpha_catalogue}

    with pytest.raises(NotFoundError):
        await buy(db_session, beta, beta_owner, paymob, alphas, now=now)
    started, payment = await buy(db_session, alpha, alpha_owner, paymob, alphas, now=now)
    await pay(db_session, alpha.id, paymob, payment, transaction=900_001_001, now=now)
    with pytest.raises(NotFoundError):
        await TopupService(db_session, tenant_id=beta.id).require_purchase(started.purchase_id)
    # Beta's limit counts none of Alpha's top-ups.
    assert (await standing(db_session, beta, LimitKey.PERIOD_AI_TURNS, at=now)).topup_limit == 0
    assert (
        await standing(db_session, alpha, LimitKey.PERIOD_AI_TURNS, at=now)
    ).topup_limit == 50_000

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                TopupPurchase(
                    tenant_id=beta.id,
                    topup_product_id=alphas.id,
                    source=TopupSource.PLATFORM_GRANT,
                    product_name="smuggled",
                    entitlement_key=AI,
                    quantity=1,
                    unit_price=Decimal("0"),
                    total_amount=Decimal("0"),
                    currency="EGP",
                    billing_period_start=now,
                    billing_period_end=later(now, days=30),
                    expires_at=later(now, days=30),
                    status=TopupStatus.PENDING,
                    reason="cross-tenant",
                )
            )
            await db_session.flush()


async def test_the_tenant_checkout_refuses_paying_a_topup_invoice_a_second_time(
    db_session: AsyncSession,
) -> None:
    """A top-up invoice is paid through its own page only - never re-opened."""
    now = base_now()
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, _ = await buy(db_session, tenant, owner, paymob, item, now=now)
    with pytest.raises(ConflictError):
        await CheckoutService(db_session, tenant_id=tenant.id, provider=paymob.provider()).start(
            invoice_id=started.invoice_id, actor=owner, now=now
        )


async def test_a_suspended_or_past_due_workspace_cannot_buy(db_session: AsyncSession) -> None:
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    item = await product(db_session, entitlement=AI, quantity=10_000)
    subscription.status = SubscriptionStatus.PAST_DUE
    await db_session.flush()
    with pytest.raises(ConflictError):
        await buy(db_session, tenant, owner, Paymob(), item, now=now)


async def test_a_topup_purchase_stays_after_a_mid_period_plan_change(
    db_session: AsyncSession,
) -> None:
    """Spec 47: an upgrade mid-period keeps a live top-up to its own expiry,
    added to the *new* base.
    """
    now = base_now()
    _, _ = await catalogue(db_session)
    await own_plan(
        db_session,
        code="business",
        price=Decimal("299.00"),
        limits={"agents": 20, "period_ai_turns": 25_000},
    )
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    item = await product(db_session, entitlement=AI, quantity=10_000)
    started, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    await apply(
        db_session,
        tenant.id,
        paymob.provider(),
        callback(payment, transaction=900_001_101),
        now=now,
    )
    first_end = subscription.current_period_end

    business = await PlanCatalog(db_session).offered()
    version = next(v for p, v in business if p.code == "business")
    upgrade = later(now, days=3)
    await SubscriptionService(db_session, tenant_id=tenant.id).apply_purchase(
        version=version, now=upgrade
    )
    purchase = await _purchase(db_session, started.purchase_id)
    assert purchase.expires_at == first_end
    state = await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=upgrade)
    assert (state.base_limit, state.topup_limit, state.limit) == (25_000, 10_000, 35_000)
    assert (
        await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=first_end)
    ).limit == 25_000


PER_KEY_QUANTITY = {
    TopupEntitlement.PERIOD_MESSAGES: 50_000,
    TopupEntitlement.PERIOD_AI_TURNS: 10_000,
    TopupEntitlement.PERIOD_CAMPAIGN_MESSAGES: 5_000,
    TopupEntitlement.STORAGE_BYTES: 25 * GIB,
    TopupEntitlement.WHATSAPP_NUMBERS: 2,
    TopupEntitlement.TEAM_MEMBERS: 5,
    TopupEntitlement.KNOWLEDGE_DOCUMENTS: 500,
}


@pytest.mark.parametrize("key", list(TopupEntitlement), ids=lambda key: key.value)
async def test_each_topup_raises_its_own_limit_once_and_leaves_the_plan_alone(
    db_session: AsyncSession, key: TopupEntitlement
) -> None:
    """Spec 30, application E2E for every one of the seven keys.

    One generic path - frozen purchase, TOPUP invoice, hosted checkout, signed
    callback, settlement, grant - with only the key, quantity and price
    differing. Before: effective = base. After: base + quantity, exactly once
    (the same callback again is a duplicate), and the plan, its version and
    its recurring price are what they were.
    """
    now = base_now()
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    paymob = Paymob()
    before = await standing(db_session, tenant, key.limit_key, at=now)
    assert (before.topup_limit, before.limit) == (0, before.base_limit)
    plan_before = (subscription.plan_id, subscription.plan_version_id)
    terms = await PlanCatalog(db_session).pinned_version(subscription)
    assert terms is not None
    price_before = terms.price

    item = await product(db_session, entitlement=key, quantity=PER_KEY_QUANTITY[key])
    _, payment = await buy(db_session, tenant, owner, paymob, item, now=now)
    signed = callback(payment, transaction=930_000_000 + list(TopupEntitlement).index(key))
    assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == APPLIED
    assert await apply(db_session, tenant.id, paymob.provider(), signed, now=now) == DUPLICATE

    after = await standing(db_session, tenant, key.limit_key, at=now)
    assert before.base_limit is not None
    assert (after.base_limit, after.topup_limit, after.limit) == (
        before.base_limit,
        PER_KEY_QUANTITY[key],
        before.base_limit + PER_KEY_QUANTITY[key],
    )
    await db_session.refresh(subscription)
    assert (subscription.plan_id, subscription.plan_version_id) == plan_before
    pinned = await PlanCatalog(db_session).pinned_version(subscription)
    assert pinned is not None and pinned.price == price_before
    grants = await db_session.scalar(
        select(func.count())
        .select_from(TopupPurchase)
        .where(TopupPurchase.tenant_id == tenant.id)
        .where(TopupPurchase.status == TopupStatus.GRANTED)
    )
    assert grants == 1
