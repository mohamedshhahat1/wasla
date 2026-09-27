"""PAY-E2E-01 and PAY-E2E-03, end to end through the real services.

Real Paymob Test proved two things the earlier suites modelled wrongly
(PAYMOB_BROWSER_E2E_VERIFICATION.md §15, §23):

* **Refunds are reported on the parent, as a running total.** Two partial
  refunds of one 99.00 payment produced two callbacks for the *same*
  transaction, `refunded_amount_cents` 3000 and then 9900. The second was
  dropped as a duplicate of the first, leaving Paymob at 99.00 refunded and
  Wasla at 30.00 with the refund request stuck open.
* **Transaction Inquiry answers with the latest transaction of the order**, and
  after a refund that is the refund child - `success: true`, `is_refund: true`
  - which was read as a collection.

So every callback below is shaped as Paymob sent it: the same parent id on
every refund notification, a cumulative total, and refund children carrying
`is_refund` and their parent. Paymob is faked at the socket only; the HMAC,
`CheckoutService`, `InvoiceSettlement`, `RefundService`, the reconciler and the
billing worker are the production code.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.billing import LimitKey, Plan, Subscription
from app.db.models.billing_incident import (
    BillingIncident,
    BillingIncidentKind,
    BillingIncidentStatus,
)
from app.db.models.custom_plan_offer import CustomPlanOfferStatus
from app.db.models.enums import PlatformRole
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.payment_event import PaymentEvent
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.user import User
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
from app.services.payment_reconciliation_service import PaymentReconciler
from app.services.refund_service import RefundService
from app.services.topup_service import TopupService
from tests.integration.test_billing_remediation_journeys import (
    HMAC_SECRET,
    T0,
    _buy,
    _callback,
    _worker,
    _workspace,
)
from tests.integration.test_billing_remediation_journeys import Paymob as JourneyPaymob
from tests.integration.test_custom_plan_offers import _accept, _custom_plan, _offer
from tests.integration.topup_harness import catalogue, product, standing, workspace

pytestmark = pytest.mark.integration

API_KEY = "an-inquiry-api-key"
PAID = 542_754_263


class Paymob(JourneyPaymob):
    """The journeys' Paymob, plus refunds and reads of one transaction by id.

    `transactions` is what ``GET /api/acceptance/transactions/{id}`` answers -
    the parent with its running total, as the real Test API did.
    """

    def __init__(self) -> None:
        super().__init__()
        self.transactions: dict[int, dict[str, Any]] = {}
        self.next_refund = 542_755_261

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "void_refund/refund" in url:
            self.requests.append({"url": url, "body": json.loads(request.content)})
            self.next_refund += 1
            return httpx.Response(
                200, json={"id": self.next_refund, "success": True, "pending": False}
            )
        if "/api/acceptance/transactions/" in url:
            self.requests.append({"url": url, "body": {}})
            found = self.transactions.get(int(url.rsplit("/", 1)[-1]))
            if found is None:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(200, json=found)
        return super().handler(request)

    def reads(self) -> list[str]:
        return [
            item["url"] for item in self.requests if "/api/acceptance/transactions/" in item["url"]
        ]

    def inquired(self) -> int:
        return sum(1 for item in self.requests if item["url"].endswith("/transaction_inquiry"))

    def refund_provider(self) -> PaymobProvider:
        return self.provider(api_key=API_KEY)


def _obj(payment: Payment, *, transaction: int) -> dict[str, Any]:
    return dict(json.loads(_callback(payment, transaction=transaction)[0])["obj"])


def _parent(payment: Payment, *, transaction: int = PAID, refunded_cents: int) -> dict[str, Any]:
    """A refund notification exactly as Paymob sent it: about the parent, cumulative."""
    obj = _obj(payment, transaction=transaction)
    obj.update(is_refunded=True, is_refund=False, refunded_amount_cents=refunded_cents)
    return obj


def _child(
    payment: Payment, *, transaction: int, amount_cents: int, parent: int = PAID
) -> dict[str, Any]:
    """The refund transaction itself, as Transaction Inquiry answers with it."""
    obj = _obj(payment, transaction=transaction)
    obj.update(
        amount_cents=amount_cents,
        refunded_amount_cents=0,
        is_refund=True,
        is_refunded=False,
        has_parent_transaction=True,
        parent_transaction=parent,
        is_standalone_payment=False,
        is_3d_secure=False,
    )
    return obj


def _signed(obj: dict[str, Any]) -> tuple[bytes, str]:
    return (
        json.dumps({"type": "TRANSACTION", "obj": obj}).encode("utf-8"),
        hmac_signature(obj, secret=HMAC_SECRET),
    )


async def _deliver(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    paymob: Paymob,
    obj: dict[str, Any],
    *,
    now: datetime,
) -> str:
    """A signed callback through the real adapter and the real settlement path."""
    provider = paymob.refund_provider()
    body, signature = _signed(obj)
    event = provider.verify_callback(payload=body, signature=signature)
    return await CheckoutService(
        session, tenant_id=tenant_id, provider=provider, default_plan_code="starter"
    ).apply(event, now=now)


def _requested(payment: Payment) -> Decimal | None:
    """The standing refund request, read now: a call, so a type checker does not
    carry an earlier assertion's narrowing past a `refresh()` that changed it."""
    return payment.refund_requested_amount


def _state(payment: Payment) -> PaymentStatus:
    return payment.status


async def _staff(session: AsyncSession) -> User:
    user = User(
        email=f"ops-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    session.add(user)
    await session.flush()
    return user


async def _operator_refund(
    session: AsyncSession,
    tenant: Tenant,
    paymob: Paymob,
    payment: Payment,
    amount: str,
    *,
    staff: User,
    now: datetime,
) -> None:
    await RefundService(session, tenant_id=tenant.id, provider=paymob.refund_provider()).refund(
        payment.id, actor=staff, amount=Decimal(amount), now=now, reason="operator refund"
    )


async def _plan_code(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    subscription = await session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    assert subscription is not None
    await session.refresh(subscription)
    plan = await session.get(Plan, subscription.plan_id)
    assert plan is not None
    return plan.code


async def _refunded_audits(session: AsyncSession, payment: Payment) -> list[str]:
    rows = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PAYMENT_REFUNDED)
            .where(AuditLog.target_id == payment.id)
            .order_by(AuditLog.occurred_at)
        )
    ).all()
    return [str((row.meta or {}).get("amount")) for row in rows]


async def _fresh(session: AsyncSession, *rows: Any) -> None:
    for row in rows:
        await session.refresh(row)


async def _bought_pro(
    session: AsyncSession, paymob: Paymob
) -> tuple[Tenant, User, Invoice, Payment]:
    tenant, owner = await _workspace(session)
    invoice, payment = await _buy(session, tenant, owner, paymob, "pro", now=T0, transaction=PAID)
    await _fresh(session, invoice, payment)
    assert invoice.status is InvoiceStatus.PAID and payment.provider_reference == str(PAID)
    return tenant, owner, invoice, payment


# =========================================== PAY-E2E-01: cumulative refunds


async def test_r1_r2_r8_r9_a_second_partial_refund_on_the_same_parent_completes_it(
    db_session: AsyncSession,
) -> None:
    """The exact real sequence: 99.00 paid, 30.00 refunded, then the remaining 69.00.

    R1 the first callback applies 30; R2 the second - same parent, total 99 -
    applies 69 instead of being a duplicate; R8 each operator request clears
    when the running total reaches it; R9 the full refund follows ADR-096: the
    payment is refunded, the invoice holds nothing and is voided (an operator
    refunded it), and the plan it bought is withdrawn.
    """
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    tenant_id = tenant.id
    staff = await _staff(db_session)

    await _operator_refund(db_session, tenant, paymob, payment, "30.00", staff=staff, now=T0)
    assert _requested(payment) == Decimal("30.00")
    outcome = await _deliver(
        db_session, tenant_id, paymob, _parent(payment, refunded_cents=3000), now=T0
    )
    await _fresh(db_session, invoice, payment)
    assert outcome == APPLIED
    assert payment.refunded_amount == Decimal("30.00")
    assert _state(payment) is PaymentStatus.SUCCEEDED
    assert _requested(payment) is None, "R8: the request was fulfilled"
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("69.00"))
    assert await _plan_code(db_session, tenant_id) == "pro", "a partial refund is goodwill"

    later = T0 + timedelta(minutes=2)
    await _operator_refund(db_session, tenant, paymob, payment, "69.00", staff=staff, now=later)
    assert _requested(payment) == Decimal("99.00")
    second = await _deliver(
        db_session, tenant_id, paymob, _parent(payment, refunded_cents=9900), now=later
    )
    await _fresh(db_session, invoice, payment)

    assert second == APPLIED, "PAY-E2E-01: the second refund used to be a duplicate"
    assert payment.refunded_amount == Decimal("99.00")
    assert _state(payment) is PaymentStatus.REFUNDED
    assert _requested(payment) is None
    assert invoice.amount_paid == Decimal("0.00")
    assert invoice.status is InvoiceStatus.VOID and invoice.voided_at == later
    assert await _plan_code(db_session, tenant_id) == "starter"
    assert await _refunded_audits(db_session, payment) == ["30.00", "69.00"]
    ids = (
        await db_session.scalars(
            select(PaymentEvent.provider_event_id)
            .where(PaymentEvent.payment_id == payment.id)
            .order_by(PaymentEvent.received_at)
        )
    ).all()
    assert ids == [f"{PAID}:succeeded", f"{PAID}:refunded:3000", f"{PAID}:refunded:9900"]
    # With nothing outstanding, the payment cannot be refunded again.
    with pytest.raises(ConflictError):
        await _operator_refund(db_session, tenant, paymob, payment, "1.00", staff=staff, now=later)


async def test_r3_r4_replaying_either_running_total_changes_nothing(
    db_session: AsyncSession,
) -> None:
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    first, second = _parent(payment, refunded_cents=3000), _parent(payment, refunded_cents=9900)
    assert await _deliver(db_session, tenant.id, paymob, first, now=T0) == APPLIED
    assert await _deliver(db_session, tenant.id, paymob, second, now=T0) == APPLIED

    for replay in (first, second, first, second):
        assert await _deliver(db_session, tenant.id, paymob, replay, now=T0) == DUPLICATE

    await _fresh(db_session, invoice, payment)
    assert payment.refunded_amount == Decimal("99.00")
    assert invoice.amount_paid == Decimal("0.00")
    assert await _refunded_audits(db_session, payment) == ["30.00", "69.00"]


async def test_r5_a_smaller_total_arriving_late_never_rolls_the_refund_back(
    db_session: AsyncSession,
) -> None:
    """99 then 30: the late 30 is stale provider state. 99 stands."""
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)

    full = _parent(payment, refunded_cents=9900)
    assert await _deliver(db_session, tenant.id, paymob, full, now=T0) == APPLIED
    stale = _parent(payment, refunded_cents=3000)
    assert await _deliver(db_session, tenant.id, paymob, stale, now=T0) == NO_CHANGE

    await _fresh(db_session, invoice, payment)
    assert payment.refunded_amount == Decimal("99.00")
    assert _state(payment) is PaymentStatus.REFUNDED
    assert invoice.amount_paid == Decimal("0.00")
    detail = await db_session.scalar(
        select(PaymentEvent.detail).where(PaymentEvent.provider_event_id == f"{PAID}:refunded:3000")
    )
    assert detail is not None and detail.startswith("Stale provider callback")
    assert await _refunded_audits(db_session, payment) == ["99.00"]


async def test_r6_three_partial_refunds_apply_each_difference_once(
    db_session: AsyncSession,
) -> None:
    """20, 50, 99 cumulative (refunds of 20, 30 and 49), from the provider's dashboard."""
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)

    for total in (2000, 5000, 9900):
        obj = _parent(payment, refunded_cents=total)
        assert await _deliver(db_session, tenant.id, paymob, obj, now=T0) == APPLIED
        assert await _deliver(db_session, tenant.id, paymob, obj, now=T0) == DUPLICATE

    await _fresh(db_session, invoice, payment)
    assert await _refunded_audits(db_session, payment) == ["20.00", "30.00", "49.00"]
    assert payment.refunded_amount == Decimal("99.00")
    assert _state(payment) is PaymentStatus.REFUNDED
    assert invoice.amount_paid == Decimal("0.00")
    # An unrequested full reversal: the invoice is open, not voided (ADR-096).
    assert invoice.status is InvoiceStatus.OPEN
    assert await _plan_code(db_session, tenant.id) == "starter"


async def test_r7_refunding_held_duplicate_money_in_parts_never_touches_the_invoice(
    db_session: AsyncSession,
) -> None:
    """P44/P60 with two partial refunds: the held payment's money goes back, the
    invoice and the payment that funded it do not move."""
    paymob = Paymob()
    tenant, owner = await _workspace(db_session)
    provider = paymob.provider()
    first = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).start(
        plan_code="pro", actor=owner, now=T0
    )
    second = await CheckoutService(db_session, tenant_id=tenant.id, provider=provider).start(
        invoice_id=first.invoice_id, actor=owner, now=T0
    )
    applied = await db_session.get(Payment, first.payment_id)
    held = await db_session.get(Payment, second.payment_id)
    invoice = await db_session.get(Invoice, first.invoice_id)
    assert applied is not None and held is not None and invoice is not None
    assert applied.id != held.id and applied.provider_order_id != held.provider_order_id

    applied_obj = _obj(applied, transaction=542_752_485)
    held_obj = _obj(held, transaction=542_752_484)
    assert await _deliver(db_session, tenant.id, paymob, applied_obj, now=T0) == APPLIED
    assert await _deliver(db_session, tenant.id, paymob, held_obj, now=T0) == REFUSED
    await _fresh(db_session, held, invoice)
    assert held.applied_at is None and invoice.amount_paid == Decimal("99.00")

    for total in (3000, 9900):
        obj = _parent(held, transaction=542_752_484, refunded_cents=total)
        assert await _deliver(db_session, tenant.id, paymob, obj, now=T0) == APPLIED

    await _fresh(db_session, applied, held, invoice)
    assert (held.status, held.refunded_amount) == (PaymentStatus.REFUNDED, Decimal("99.00"))
    assert (applied.status, applied.refunded_amount) == (PaymentStatus.SUCCEEDED, Decimal("0"))
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("99.00"))
    assert await _plan_code(db_session, tenant.id) == "pro"


async def test_r10_another_workspace_cannot_reach_the_refund(db_session: AsyncSession) -> None:
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    other, _ = await _workspace(db_session, catalogue=False)
    staff = await _staff(db_session)

    obj = _parent(payment, refunded_cents=9900)
    assert await _deliver(db_session, other.id, paymob, obj, now=T0) == UNMATCHED
    with pytest.raises(NotFoundError):
        await RefundService(db_session, tenant_id=other.id, provider=paymob.provider()).refund(
            payment.id, actor=staff, amount=Decimal("1.00"), now=T0
        )

    await _fresh(db_session, invoice, payment)
    assert payment.refunded_amount == Decimal("0.00")
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("99.00"))
    assert [item for item in paymob.requests if "void_refund" in item["url"]] == []


async def test_a_refund_of_another_transaction_on_the_same_order_is_not_taken_off_the_invoice(
    db_session: AsyncSession,
) -> None:
    """One page paid twice (BILL-15): refunding the second collection must not
    subtract from the invoice the first one funded."""
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    second = _obj(payment, transaction=PAID + 1)
    assert await _deliver(db_session, tenant.id, paymob, second, now=T0) == REFUSED

    reversal = _parent(payment, transaction=PAID + 1, refunded_cents=9900)
    assert await _deliver(db_session, tenant.id, paymob, reversal, now=T0) == MISMATCHED

    await _fresh(db_session, invoice, payment)
    assert payment.refunded_amount == Decimal("0.00")
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("99.00"))


# ================================= PAY-E2E-03: refund children and inquiry


async def test_a_refund_child_arriving_as_a_callback_is_never_a_second_payment(
    db_session: AsyncSession,
) -> None:
    """Before the fix a 99.00 child on a paid order read as a second success on
    it (a duplicate-payment incident); a 69.00 one as an amount mismatch."""
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)

    for amount in (9900, 6900):
        child = _child(payment, transaction=542_755_547 + amount, amount_cents=amount)
        assert await _deliver(db_session, tenant.id, paymob, child, now=T0) == NO_CHANGE

    await _fresh(db_session, invoice, payment)
    assert payment.refunded_amount == Decimal("0.00")
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("99.00"))
    incidents = await db_session.scalar(
        select(func.count())
        .select_from(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
    )
    assert incidents == 0


async def test_i1_a_lost_callback_without_a_refund_still_settles_by_inquiry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the refund fix must not stop normal lost-callback recovery."""
    paymob = Paymob()
    tenant, owner = await _workspace(db_session)
    tenant_id = tenant.id
    provider = paymob.refund_provider()
    started = await CheckoutService(db_session, tenant_id=tenant_id, provider=provider).start(
        plan_code="pro", actor=owner, now=T0
    )
    await db_session.commit()
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    await db_session.refresh(payment)
    paymob.inquiry = _obj(payment, transaction=PAID)

    await _worker(db_session, monkeypatch, provider)._reconcile(
        now=payment.created_at + timedelta(hours=1)
    )

    invoice = await db_session.get(Invoice, started.invoice_id)
    assert invoice is not None
    await _fresh(db_session, payment, invoice)
    assert _state(payment) is PaymentStatus.SUCCEEDED and payment.applied_at is not None
    assert invoice.status is InvoiceStatus.PAID
    assert await _plan_code(db_session, tenant_id) == "pro"
    assert paymob.reads() == [], "an ordinary answer needs no parent read"


@pytest.mark.parametrize(("refunded_cents", "held"), [(9900, Decimal("0")), (3000, Decimal("69"))])
async def test_i7_paid_then_refunded_before_reconciliation_settles_nothing(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    refunded_cents: int,
    held: Decimal,
) -> None:
    """The dangerous case: the collection callback is lost, the payment is refunded at
    Paymob, and only then does reconciliation ask. Inquiry answers with the refund
    child. Before the fix a full refund child settled the invoice with money that
    had already gone back; now nothing is settled or granted, the provider's state
    is recorded, and an incident says so."""
    paymob = Paymob()
    tenant, owner = await _workspace(db_session)
    tenant_id = tenant.id
    provider = paymob.refund_provider()
    started = await CheckoutService(db_session, tenant_id=tenant_id, provider=provider).start(
        plan_code="pro", actor=owner, now=T0
    )
    await db_session.commit()
    payment = await db_session.get(Payment, started.payment_id)
    invoice = await db_session.get(Invoice, started.invoice_id)
    assert payment is not None and invoice is not None
    await _fresh(db_session, payment, invoice)
    paymob.inquiry = _child(payment, transaction=PAID + 700, amount_cents=refunded_cents)
    paymob.transactions[PAID] = _parent(payment, refunded_cents=refunded_cents)

    worker = _worker(db_session, monkeypatch, provider)
    await worker._reconcile(now=payment.created_at + timedelta(hours=1))
    await _fresh(db_session, payment, invoice)

    assert invoice.status is InvoiceStatus.OPEN, "no invoice paid from refunded money"
    assert (invoice.amount_paid, invoice.paid_at) == (Decimal("0.00"), None)
    assert await _plan_code(db_session, tenant_id) == "starter", "no subscription activation"
    assert payment.applied_at is None
    assert payment.provider_reference == str(PAID)
    assert payment.refunded_amount == Decimal(refunded_cents) / 100
    assert _state(payment) is (PaymentStatus.REFUNDED if held == 0 else PaymentStatus.SUCCEEDED)
    incident = await db_session.scalar(
        select(BillingIncident).where(BillingIncident.payment_id == payment.id)
    )
    assert incident is not None and incident.kind is BillingIncidentKind.REFUSED_SETTLEMENT
    assert incident.amount == held
    assert incident.status is (
        BillingIncidentStatus.RESOLVED if held == 0 else BillingIncidentStatus.OPEN
    )
    assert paymob.reads() == [f"https://accept.paymob.com/api/acceptance/transactions/{PAID}"]

    # Resolved: the next pass does not ask about it again.
    asked = paymob.inquired()
    await worker._reconcile(now=payment.created_at + timedelta(hours=2))
    assert paymob.inquired() == asked
    await _fresh(db_session, payment)
    # And the original success callback, arriving late, cannot settle it either.
    late = await _deliver(db_session, tenant_id, paymob, _obj(payment, transaction=PAID), now=T0)
    assert late in (NO_CHANGE, REFUSED)
    await _fresh(db_session, invoice)
    assert invoice.status is InvoiceStatus.OPEN


async def test_i7_a_refunded_topup_page_is_never_granted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = T0
    await catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now)
    tenant_id = tenant.id
    paymob = Paymob()
    provider = paymob.refund_provider()
    item = await product(db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=2_000)
    started = await TopupService(
        db_session,
        tenant_id=tenant_id,
        checkout=CheckoutService(db_session, tenant_id=tenant_id, provider=provider),
    ).start_checkout(item.id, actor=owner, idempotency_key=None, now=now)
    await db_session.commit()
    payment = await db_session.get(Payment, started.payment_id)
    assert payment is not None
    await db_session.refresh(payment)
    cents = int(payment.amount * 100)
    paymob.inquiry = _child(payment, transaction=PAID + 800, amount_cents=cents)
    paymob.transactions[PAID] = _parent(payment, refunded_cents=cents)

    await _worker(db_session, monkeypatch, provider)._reconcile(
        now=payment.created_at + timedelta(hours=1)
    )

    purchase = await db_session.get(TopupPurchase, started.purchase_id)
    assert purchase is not None
    await db_session.refresh(purchase)
    assert purchase.status is not TopupStatus.GRANTED and purchase.granted_at is None
    await db_session.refresh(tenant)
    limit = (await standing(db_session, tenant, LimitKey.PERIOD_AI_TURNS, at=now)).limit
    assert limit == 5_000, "no top-up grant"


async def test_i7_a_refunded_custom_offer_page_is_never_activated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = T0
    await catalogue(db_session)
    tenant, owner, subscription = await workspace(db_session, now=now)
    staff = await _staff(db_session)
    paymob = Paymob()
    provider = paymob.refund_provider()
    _, v1 = await _custom_plan(db_session, tenant, staff, now=now)
    offer = await _offer(db_session, tenant, staff, v1, now=now)
    _, payment, invoice = await _accept(db_session, tenant, owner, provider, offer, now=now)
    before = subscription.plan_version_id
    await db_session.commit()
    await _fresh(db_session, payment, subscription)
    cents = int(payment.amount * 100)
    paymob.inquiry = _child(payment, transaction=PAID + 900, amount_cents=cents)
    paymob.transactions[PAID] = _parent(payment, refunded_cents=cents)

    await _worker(db_session, monkeypatch, provider)._reconcile(
        now=payment.created_at + timedelta(hours=1)
    )

    await _fresh(db_session, offer, invoice, subscription)
    assert offer.status is CustomPlanOfferStatus.PENDING_PAYMENT, "no custom offer activation"
    assert invoice.status is InvoiceStatus.OPEN
    assert subscription.plan_version_id == before


async def test_i8_a_lost_refund_callback_is_recovered_by_the_refund_sweep(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applied payment, an operator's full refund accepted by Paymob, the callback
    lost. The sweep reads the payment's transaction, sees the running total and
    applies it through the same reversal path; a late callback is then a duplicate."""
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    tenant_id = tenant.id
    staff = await _staff(db_session)
    await _operator_refund(db_session, tenant, paymob, payment, "99.00", staff=staff, now=T0)
    paymob.transactions[PAID] = _parent(payment, refunded_cents=9900)

    worker = _worker(db_session, monkeypatch, paymob.refund_provider())
    handled = await worker._reconcile(now=T0 + timedelta(hours=1))
    await _fresh(db_session, invoice, payment)

    assert handled >= 1
    assert payment.refunded_amount == Decimal("99.00")
    assert _state(payment) is PaymentStatus.REFUNDED
    assert _requested(payment) is None, "no stuck refund request"
    assert invoice.status is InvoiceStatus.VOID
    assert await _plan_code(db_session, tenant_id) == "starter"
    payments = await db_session.scalar(
        select(func.count()).select_from(Payment).where(Payment.tenant_id == tenant_id)
    )
    assert payments == 1, "reconciliation creates no payment"

    late = await _deliver(
        db_session, tenant_id, paymob, _parent(payment, refunded_cents=9900), now=T0
    )
    assert late == DUPLICATE
    reads = len(paymob.reads())
    assert await worker._reconcile(now=T0 + timedelta(hours=3)) == 0
    assert len(paymob.reads()) == reads, "nothing outstanding, nothing asked"


async def test_i6_a_partial_refund_whose_callback_was_lost_converges_without_resettling(
    db_session: AsyncSession,
) -> None:
    paymob = Paymob()
    tenant, _, invoice, payment = await _bought_pro(db_session, paymob)
    staff = await _staff(db_session)
    await _operator_refund(db_session, tenant, paymob, payment, "30.00", staff=staff, now=T0)
    paymob.transactions[PAID] = _parent(payment, refunded_cents=3000)

    reconciler = PaymentReconciler(
        session=db_session, provider=paymob.refund_provider(), default_plan_code="starter"
    )
    verdict = await reconciler.reconcile_payment(payment.id, now=T0 + timedelta(hours=1))
    await _fresh(db_session, invoice, payment)

    assert verdict == "refund_applied"
    assert payment.refunded_amount == Decimal("30.00")
    assert _requested(payment) is None
    assert (invoice.status, invoice.amount_paid) == (InvoiceStatus.PAID, Decimal("69.00"))
    settled = await db_session.scalar(
        select(func.count())
        .select_from(PaymentEvent)
        .where(PaymentEvent.payment_id == payment.id)
        .where(PaymentEvent.outcome == APPLIED)
        .where(PaymentEvent.event_type == "transaction.succeeded")
    )
    assert settled == 1, "the invoice is not settled a second time"
    assert await reconciler.reconcile_payment(payment.id, now=T0 + timedelta(hours=2)) == (
        "no_change"
    )


async def test_the_provider_and_ledger_refund_totals_can_be_compared(
    db_session: AsyncSession,
) -> None:
    """The check the internal invariant ledger could not make (PAY-E2E-01):
    Paymob 99.00 refunded, Wasla 30.00, found by comparing the two."""
    paymob = Paymob()
    tenant, _, _, payment = await _bought_pro(db_session, paymob)
    obj = _parent(payment, refunded_cents=3000)
    assert await _deliver(db_session, tenant.id, paymob, obj, now=T0) == APPLIED
    paymob.transactions[PAID] = _parent(payment, refunded_cents=9900)
    reconciler = PaymentReconciler(session=db_session, provider=paymob.refund_provider())

    before = await reconciler.compare_refund_state(payment.id)
    assert (before.provider_refunded, before.ledger_refunded) == (Decimal("99"), Decimal("30"))
    assert before.matches is False

    assert await reconciler.reconcile_payment(payment.id, now=T0) == "refund_applied"
    after = await reconciler.compare_refund_state(payment.id)
    assert after.matches is True

    unreachable = PaymentReconciler(session=db_session, provider=paymob.provider())
    unknown = await unreachable.compare_refund_state(payment.id)
    assert unknown.matches is None and unknown.verdict == "unsupported"


# ================================ PAY-E2E-02: a purchase is still a purchase


async def test_a_paid_purchase_still_records_purchase_settled_by_the_system(
    db_session: AsyncSession,
) -> None:
    paymob = Paymob()
    tenant, _, invoice, _ = await _bought_pro(db_session, paymob)
    rows = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id)
            .where(AuditLog.action == AuditAction.SUBSCRIPTION_PLAN_CHANGED)
        )
    ).all()
    settled = [row for row in rows if (row.meta or {}).get("reason") == "purchase_settled"]
    assert len(settled) == 1
    assert settled[0].actor_kind is AuditActorKind.SYSTEM and settled[0].actor_id is None
    assert settled[0].meta is not None
    assert settled[0].meta["plan_version_id"] == str(invoice.plan_version_id)
