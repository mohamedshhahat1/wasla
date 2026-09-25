"""Shared billing fixtures that follow the remediated contract.

Two things every billing test now needs and used to be able to skip:

* **An owner.** Automatic renewals send the workspace owner's e-mail as the
  billing contact (BILL-05); a workspace with no owner is refused before any
  request reaches the provider, exactly as production refuses it.
* **A renewal that looks like one.** Renewals are billed in advance for the
  subscription's *current* period, at the plan version the subscription is
  pinned to (BILL-02, BILL-03). An invoice for the period that already ended,
  or one with no version, is not a renewal and is never collected
  automatically.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import Plan, PlanVersion, Subscription
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.invoice import Invoice, InvoicePurpose, InvoiceStatus
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.plan_catalog import PlanCatalog


async def add_owner(
    session: AsyncSession,
    tenant: Tenant,
    *,
    email: str | None = None,
    full_name: str = "Billing Owner",
) -> User:
    """An active owner for `tenant` - the workspace's billing contact."""
    user = User(
        email=email or f"owner-{uuid.uuid4().hex[:10]}@example.com",
        full_name=full_name,
        hashed_password="x",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    session.add(
        Membership(
            tenant_id=tenant.id,
            user_id=user.id,
            role=TenantRole.TENANT_OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()
    return user


async def version_of(session: AsyncSession, plan: Plan) -> PlanVersion:
    """The plan's current version, materialised if the row predates versions."""
    version = await PlanCatalog(session).current_version(plan)
    assert version is not None
    return version


async def pin(session: AsyncSession, subscription: Subscription, plan: Plan) -> PlanVersion:
    """Pin a hand-built subscription to its plan's current version."""
    version = await version_of(session, plan)
    subscription.plan_version_id = version.id
    if subscription.billing_anchor_at is None:
        subscription.billing_anchor_at = subscription.current_period_start
    await session.flush()
    return version


async def renewal_invoice(
    session: AsyncSession,
    *,
    subscription: Subscription,
    plan: Plan,
    issued_at: datetime | None = None,
    amount_due: Decimal | None = None,
    status: InvoiceStatus = InvoiceStatus.OPEN,
    collection_attempts: int = 0,
) -> Invoice:
    """The sweep's advance bill for the subscription's current period."""
    version = await pin(session, subscription, plan)
    invoice = Invoice(
        tenant_id=subscription.tenant_id,
        subscription_id=subscription.id,
        status=status,
        purpose=InvoicePurpose.RENEWAL,
        plan_code=plan.code,
        plan_version_id=version.id,
        amount_due=amount_due if amount_due is not None else version.price,
        amount_paid=Decimal("0.00"),
        currency=version.currency,
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
        issued_at=issued_at if issued_at is not None else subscription.current_period_start,
        lines=[],
        collection_attempts=collection_attempts,
    )
    session.add(invoice)
    await session.flush()
    return invoice


LEDGER_TEARDOWN = (
    "DELETE FROM billing_incidents WHERE tenant_id = ANY(:ids)",
    "DELETE FROM billing_adjustments WHERE tenant_id = ANY(:ids)",
    "DELETE FROM payment_events WHERE payment_id IN "
    "(SELECT id FROM payments WHERE tenant_id = ANY(:ids))",
    "DELETE FROM payments WHERE tenant_id = ANY(:ids)",
    "DELETE FROM invoices WHERE tenant_id = ANY(:ids)",
)


async def erase_ledger(executor: object, tenant_ids: list[uuid.UUID]) -> None:
    """Delete a test tenant's financial ledger so the tenant row can go.

    Tests that commit clean up after themselves by deleting tenants, and that
    used to take invoices and payments with them by cascade. The ledger no
    longer cascades (BILL-19) - a tenant with financial history cannot be
    deleted by accident - so a teardown erases it first, explicitly, which is
    exactly the deliberate step production now demands.
    """
    from sqlalchemy import text

    for statement in LEDGER_TEARDOWN:
        await executor.execute(text(statement), {"ids": list(tenant_ids)})  # type: ignore[attr-defined]
