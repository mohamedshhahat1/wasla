"""BILL-08: count-based limits hold under real concurrency.

The audit's probe P7 created two agents at a limit of one: `require()` read the
count, the creating request wrote the row later, and two requests reading
"zero of one used" at once both went ahead. The guard every creating route now
declares - `EntitlementService.reserve_or_refuse` - takes a per-workspace
advisory lock that lives until the request commits, so the second request
counts the first one's row.

Driven exactly as a request is: one committing session per request, the guard,
then the row, then commit - two of them at once against a limit of one, for
each count-based resource. Real PostgreSQL, real transactions, no sleeps that
the result depends on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.exceptions import PlanLimitExceededError
from app.db.models.agent import Agent
from app.db.models.billing import BillingInterval, LimitKey, Plan, Subscription, SubscriptionStatus
from app.db.models.enums import TenantRole
from app.db.models.invitation import TenantInvitation
from app.db.models.knowledge import Document, KnowledgeBase
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from tests.billing_fixtures import erase_ledger

pytestmark = pytest.mark.integration

LIMITS = {
    LimitKey.AGENTS.value: 1,
    LimitKey.WHATSAPP_NUMBERS.value: 1,
    # One seat: the owner is not a member row here, so an invitation fills it.
    LimitKey.TEAM_MEMBERS.value: 1,
    LimitKey.KNOWLEDGE_DOCUMENTS.value: 1,
}


@pytest_asyncio.fixture
async def maker(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=6, max_overflow=4)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[uuid.UUID]:
    code = f"race-{uuid.uuid4().hex[:8]}"
    async with maker() as session:
        tenant = Tenant(name="Race", slug=f"race-{uuid.uuid4().hex[:10]}")
        plan = Plan(
            code=code,
            name="Race",
            price=Decimal("0.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits=dict(LIMITS),
        )
        session.add_all([tenant, plan])
        await session.flush()
        # Published and pinned here, before the race. Left to the racing
        # requests, both would materialise version 1 at once and the second
        # would wait on the first's uncommitted row - serialising them by
        # accident and hiding a missing lock (mutation R-BILL-08).
        version = await PlanCatalog(session).current_version(plan)
        assert version is not None
        now = datetime.now(UTC)
        session.add(
            Subscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                plan_version_id=version.id,
                status=SubscriptionStatus.ACTIVE,
                current_period_start=now,
                current_period_end=now + timedelta(days=30),
            )
        )
        session.add(KnowledgeBase(tenant_id=tenant.id, name="kb"))
        await session.commit()
        tenant_id, plan_id = tenant.id, plan.id
    try:
        yield tenant_id
    finally:
        async with maker() as session:
            await erase_ledger(session, [tenant_id])
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.execute(delete(Plan).where(Plan.id == plan_id))
            await session.commit()


def _agent(tenant_id: uuid.UUID, index: int) -> object:
    return Agent(tenant_id=tenant_id, name=f"agent-{index}", model="gpt", system_prompt="x")


def _number(tenant_id: uuid.UUID, index: int) -> object:
    return WhatsAppAccount(
        tenant_id=tenant_id,
        phone_number_id=f"race-{uuid.uuid4().hex[:12]}",
        waba_id=f"waba-{index}",
        display_phone_number=f"+2010000000{index}",
    )


def _invitation(tenant_id: uuid.UUID, index: int) -> object:
    return TenantInvitation(
        tenant_id=tenant_id,
        email=f"invitee-{index}-{uuid.uuid4().hex[:6]}@example.com",
        role=TenantRole.MEMBER,
        token_hash=uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


async def _document(session: AsyncSession, tenant_id: uuid.UUID, index: int) -> object:
    base = await session.scalar(
        select(KnowledgeBase.id).where(KnowledgeBase.tenant_id == tenant_id)
    )
    return Document(
        tenant_id=tenant_id,
        knowledge_base_id=base,
        title=f"doc-{index}",
        content_hash=uuid.uuid4().hex,
    )


async def _race(
    maker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    key: LimitKey,
    build: Callable[[AsyncSession, int], Awaitable[object]],
) -> list[str]:
    """Two "requests" at once: guard, write, commit. Returns how each ended."""
    ready = asyncio.Barrier(2)

    async def request(index: int) -> str:
        async with maker() as session:
            await ready.wait()
            try:
                await EntitlementService(session, tenant_id=tenant_id).reserve_or_refuse(key)
            except PlanLimitExceededError:
                await session.rollback()
                return "refused"
            # Hold the lock across a real gap, as a slow route would.
            await asyncio.sleep(0.2)
            session.add(await build(session, index))
            await session.commit()
            return "created"

    return sorted(await asyncio.gather(request(1), request(2)))


async def _count(maker: async_sessionmaker[AsyncSession], model: Any, tenant_id: uuid.UUID) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            )
            or 0
        )


async def _built(
    builder: Callable[[uuid.UUID, int], object], tenant_id: uuid.UUID
) -> Callable[[AsyncSession, int], Awaitable[object]]:
    async def build(session: AsyncSession, index: int) -> object:
        return builder(tenant_id, index)

    return build


@pytest.mark.parametrize(
    ("key", "model", "builder"),
    [
        (LimitKey.AGENTS, Agent, _agent),
        (LimitKey.WHATSAPP_NUMBERS, WhatsAppAccount, _number),
        (LimitKey.TEAM_MEMBERS, TenantInvitation, _invitation),
    ],
)
async def test_two_creations_at_a_limit_of_one_leave_one(
    maker: async_sessionmaker[AsyncSession],
    workspace: uuid.UUID,
    key: LimitKey,
    model: type,
    builder: Callable[[uuid.UUID, int], object],
) -> None:
    outcomes = await _race(maker, workspace, key, await _built(builder, workspace))

    assert outcomes == ["created", "refused"]
    assert await _count(maker, model, workspace) == 1


async def test_two_document_submissions_at_a_limit_of_one_leave_one(
    maker: async_sessionmaker[AsyncSession], workspace: uuid.UUID
) -> None:
    async def build(session: AsyncSession, index: int) -> object:
        return await _document(session, workspace, index)

    outcomes = await _race(maker, workspace, LimitKey.KNOWLEDGE_DOCUMENTS, build)

    assert outcomes == ["created", "refused"]
    assert await _count(maker, Document, workspace) == 1


async def test_every_creating_route_uses_the_locked_guard() -> None:
    """The routes declare `require_entitlement`, and it must reserve, not merely check."""
    import inspect

    from app.api import dependencies

    source = inspect.getsource(dependencies.require_entitlement)
    assert "reserve_or_refuse" in source
    assert ".require(" not in source
