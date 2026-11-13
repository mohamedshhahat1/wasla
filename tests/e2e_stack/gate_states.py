"""Put an e2e workspace into a gate state the API cannot reach in a few calls (FE-3, EC-04).

    python -m tests.e2e_stack.gate_states trialing <slug> <days>   trial ends in <days> days
    python -m tests.e2e_stack.gate_states past-due <slug>          past due, a renewal owed
    python -m tests.e2e_stack.gate_states ai-allowance-used <slug> this period's AI turns used up

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that). The journeys run it with
``docker exec wasla-fe-e2e-api``. Every other gate state FE-3 draws is reached
through the API as a person or staff member would - suspend / restore, withdrawing
a membership, a capacity top-up granted then withdrawn (a reduction), connecting
the plan's last channel slot.

Each command writes through the application's own models and repositories and
prints one JSON line with what the workspace's gates now say, read back by the
same function ``GET /api/v1/workspace/gates`` answers with.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    import uuid

    from app.db.models import Tenant
    from app.db.models.billing import Subscription

USAGE = (
    "usage: python -m tests.e2e_stack.gate_states "
    "(trialing <slug> <days> | past-due <slug> | ai-allowance-used <slug>)"
)


async def _tenant(session: AsyncSession, slug: str) -> Tenant:
    from app.db.models import Tenant

    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"no workspace with the slug {slug!r}")
    return tenant


async def _subscription(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    from app.repositories.billing_repository import SubscriptionRepository

    subscription = await SubscriptionRepository(session, tenant_id=tenant_id).get()
    if subscription is None:
        raise SystemExit("the workspace has no subscription (the default plan starts one)")
    return subscription


async def trialing(session: AsyncSession, slug: str, days: int) -> None:
    """The subscription is a trial that ends in ``days`` days."""
    from app.db.models.billing import SubscriptionStatus

    tenant = await _tenant(session, slug)
    subscription = await _subscription(session, tenant.id)
    ends = datetime.now(UTC) + timedelta(days=days)
    subscription.status = SubscriptionStatus.TRIALING
    subscription.trial_ends_at = ends
    subscription.current_period_end = ends


async def past_due(session: AsyncSession, slug: str) -> None:
    """The subscription is past due and one issued renewal is still owed."""
    from app.db.models.billing import SubscriptionStatus
    from app.db.models.invoice import InvoicePurpose, InvoiceStatus
    from app.repositories.invoice_repository import InvoiceRepository

    tenant = await _tenant(session, slug)
    subscription = await _subscription(session, tenant.id)
    subscription.status = SubscriptionStatus.PAST_DUE
    moment = datetime.now(UTC)
    invoice = InvoiceRepository(session, tenant_id=tenant.id).create(
        subscription_id=subscription.id,
        plan_code="e2e-renewal",
        amount_due=Decimal("99.00"),
        currency="EGP",
        period_start=moment,
        period_end=moment + timedelta(days=30),
        lines=[],
        status=InvoiceStatus.OPEN,
        purpose=InvoicePurpose.RENEWAL,
    )
    invoice.issued_at = moment


async def ai_allowance_used(session: AsyncSession, slug: str) -> None:
    """This period's AI turns are all used: one usage event per remaining turn."""
    from app.db.models.billing import LimitKey
    from app.db.models.usage import UsageEventType
    from app.services.entitlement_service import EntitlementService
    from app.services.usage_service import UsageRecorder

    tenant = await _tenant(session, slug)
    entitlement = await EntitlementService(session, tenant_id=tenant.id).check(
        LimitKey.PERIOD_AI_TURNS, additional=0
    )
    if entitlement.remaining is None:
        raise SystemExit("the workspace's AI turns are unlimited; nothing can use them up")
    if entitlement.remaining > 0:
        UsageRecorder(session, tenant_id=tenant.id).record(
            UsageEventType.AI_TURN, quantity=entitlement.remaining
        )


async def _gates(session: AsyncSession, slug: str) -> dict[str, object]:
    from app.core.config import get_settings
    from app.services.workspace_gates_service import read_workspace_gates

    tenant = await _tenant(session, slug)
    gates = await read_workspace_gates(session, tenant=tenant, settings=get_settings())
    return gates.model_dump(mode="json")


def parse(argv: list[str]) -> tuple[str, str, int | None]:
    """(command, slug, days) or the usage, before any connection is opened."""
    if len(argv) == 3 and argv[0] == "trialing" and argv[2].isdigit():
        return argv[0], argv[1], int(argv[2])
    if len(argv) == 2 and argv[0] in {"past-due", "ai-allowance-used"}:
        return argv[0], argv[1], None
    raise SystemExit(USAGE)


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, slug, days = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            if command == "trialing" and days is not None:
                await trialing(session, slug, days)
            elif command == "past-due":
                await past_due(session, slug)
            else:
                await ai_allowance_used(session, slug)
            await session.commit()
        async with database.session() as session:
            return await _gates(session, slug)
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    print(json.dumps(asyncio.run(run(argv))))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])
