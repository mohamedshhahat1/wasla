"""Shared machinery for the custom-plan and top-up suites (ADR-113).

Paymob is faked at the socket only, exactly as the billing journeys do it: its
intention responses carry an order id derived from our own reference
(`tests/paymob_orders.py`), and callbacks are signed with the real adapter's
HMAC and applied through the real `CheckoutService` and `InvoiceSettlement`. So
a top-up here is bought, paid and granted by the code production runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import LimitKey, Plan, Subscription
from app.db.models.invoice import Payment
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupProduct, TopupScope, TopupValidity
from app.db.models.user import User
from app.integrations.billing.paymob import PaymobProvider, hmac_signature
from app.services.checkout_service import CheckoutService
from app.services.entitlement_service import Entitlement, EntitlementService
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from app.services.topup_service import StartedTopupCheckout, TopupService
from tests.billing_fixtures import add_owner
from tests.integration.plan_catalogue import own_plan
from tests.paymob_orders import CARD_INTEGRATION_ID, MOTO_INTEGRATION_ID, order_from_request

HMAC_SECRET = "topup-hmac-secret"
GIB = 1024**3


class Paymob:
    """Paymob at the socket: intentions and refunds, counted."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def intentions(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if "/v1/intention" in item["url"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append({"url": str(request.url), "body": body})
        url = str(request.url)
        if "/v1/intention" in url:
            return httpx.Response(
                201,
                json={
                    "id": f"pi_test_{uuid.uuid4().hex[:12]}",
                    "client_secret": f"egy_csk_test_{uuid.uuid4().hex[:12]}",
                    "intention_order_id": order_from_request(request),
                },
            )
        if "void_refund/refund" in url:
            return httpx.Response(200, json={"id": 880_000_001, "success": True, "pending": True})
        return httpx.Response(404, json={})

    def provider(self) -> PaymobProvider:
        return PaymobProvider(
            secret_key="egy_sk_test_topup",
            public_key="egy_pk_test_topup",
            hmac_secret=HMAC_SECRET,
            integration_ids=[CARD_INTEGRATION_ID],
            moto_integration_id=MOTO_INTEGRATION_ID,
            transport=httpx.MockTransport(self.handler),
        )


def callback(
    payment: Payment,
    *,
    transaction: int,
    success: bool = True,
    refunded_cents: int | None = None,
    amount_cents: int | None = None,
) -> tuple[bytes, str]:
    """A signed Paymob TRANSACTION callback about `payment`'s order."""
    refund = refunded_cents is not None
    obj: dict[str, Any] = {
        "id": transaction,
        "pending": False,
        "amount_cents": (amount_cents if amount_cents is not None else int(payment.amount * 100)),
        "success": success,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": refund,
        "is_3d_secure": True,
        "integration_id": CARD_INTEGRATION_ID,
        "has_parent_transaction": refund,
        "order": {"id": int(str(payment.provider_order_id)), "merchant_order_id": str(payment.id)},
        "is_live": False,
        "created_at": "2026-09-01T10:00:00.000000",
        "currency": "EGP",
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": not success,
        "owner": 302852,
    }
    if refund:
        obj["refunded_amount_cents"] = refunded_cents
        obj["parent_transaction"] = int(payment.provider_reference or 0)
    body = json.dumps({"type": "TRANSACTION", "obj": obj}).encode("utf-8")
    return body, hmac_signature(obj, secret=HMAC_SECRET)


async def apply(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    provider: PaymobProvider,
    signed: tuple[bytes, str],
    *,
    now: datetime,
) -> str:
    """Verify a signed callback with the real adapter and settle it."""
    event = provider.verify_callback(payload=signed[0], signature=signed[1])
    return await CheckoutService(
        session, tenant_id=tenant_id, provider=provider, default_plan_code="starter"
    ).apply(event, now=now)


async def pay(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    paymob: Paymob,
    payment: Payment,
    *,
    transaction: int,
    now: datetime,
) -> str:
    """Paymob reports a successful transaction on `payment`'s page."""
    signed = callback(payment, transaction=transaction)
    return await apply(session, tenant_id, paymob.provider(), signed, now=now)


def base_now() -> datetime:
    """Now, to the second: tests that go through HTTP use the real clock."""
    return datetime.now(UTC).replace(microsecond=0)


async def catalogue(session: AsyncSession) -> tuple[Plan, Plan]:
    """Starter (free) and Pro (99 EGP) with every one of the seven keys limited."""
    starter = await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={
            "agents": 1,
            "period_messages": 1_000,
            "period_ai_turns": 100,
            "period_campaign_messages": 100,
            "storage_bytes": GIB,
            "whatsapp_numbers": 1,
            "team_members": 2,
            "knowledge_documents": 10,
        },
    )
    pro = await own_plan(
        session,
        code="pro",
        price=Decimal("99.00"),
        limits={
            "agents": 5,
            "period_messages": 10_000,
            "period_ai_turns": 5_000,
            "period_campaign_messages": 1_000,
            "storage_bytes": 10 * GIB,
            "whatsapp_numbers": 1,
            "team_members": 10,
            "knowledge_documents": 500,
        },
    )
    return starter, pro


async def workspace(
    session: AsyncSession,
    *,
    now: datetime,
    plan_code: str = "pro",
    name: str = "Topup Co",
) -> tuple[Tenant, User, Subscription]:
    """A workspace with an owner, holding `plan_code` for a period starting `now`.

    A priced plan is applied as a settled purchase would apply it - the money
    path for plans is proved by the billing suites; here it is the ground the
    top-ups stand on.
    """
    tenant = Tenant(name=name, slug=f"topup-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await add_owner(session, tenant)
    service = SubscriptionService(session, tenant_id=tenant.id)
    await service.start(plan_code="starter", now=now, self_service=False)
    if plan_code != "starter":
        plan = await session.scalar(select(Plan).where(Plan.code == plan_code))
        assert plan is not None
        version = await PlanCatalog(session).current_version(plan, at=now)
        assert version is not None
        await service.apply_purchase(version=version, now=now)
    subscription = await session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    return tenant, owner, subscription


async def product(
    session: AsyncSession,
    *,
    entitlement: TopupEntitlement,
    quantity: int,
    price: str = "200.00",
    tenant: Tenant | None = None,
    code: str | None = None,
    active: bool = True,
    public: bool = True,
) -> TopupProduct:
    row = TopupProduct(
        code=code or f"tu-{uuid.uuid4().hex[:10]}",
        name=f"{entitlement.value} +{quantity}",
        entitlement_key=entitlement,
        quantity=quantity,
        price=Decimal(price),
        currency="EGP",
        scope=TopupScope.TENANT if tenant is not None else TopupScope.GLOBAL,
        tenant_id=tenant.id if tenant is not None else None,
        is_active=active,
        is_public=public,
        validity_policy=TopupValidity.CURRENT_PERIOD_END,
    )
    session.add(row)
    await session.flush()
    return row


async def buy(
    session: AsyncSession,
    tenant: Tenant,
    owner: User,
    paymob: Paymob,
    item: TopupProduct,
    *,
    now: datetime,
    idempotency_key: str | None = None,
) -> tuple[StartedTopupCheckout, Payment]:
    """Open a top-up checkout through the real service; the page is not yet paid."""
    checkout = CheckoutService(session, tenant_id=tenant.id, provider=paymob.provider())
    started = await TopupService(session, tenant_id=tenant.id, checkout=checkout).start_checkout(
        item.id, actor=owner, idempotency_key=idempotency_key, now=now
    )
    payment = await session.get(Payment, started.payment_id)
    assert payment is not None
    return started, payment


async def standing(
    session: AsyncSession, tenant: Tenant, key: LimitKey, *, at: datetime
) -> Entitlement:
    """Where `tenant` stands against `key` at `at`, from the one authority."""
    return await EntitlementService(
        session, tenant_id=tenant.id, default_plan_code="starter", clock=lambda: at
    ).check(key, additional=0)


def later(moment: datetime, **delta: float) -> datetime:
    return moment + timedelta(**delta)


async def held(session: AsyncSession, model: type[Any], tenant_id: uuid.UUID) -> int:
    """How many rows of `model` one workspace holds."""
    from sqlalchemy import func

    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )
        or 0
    )
