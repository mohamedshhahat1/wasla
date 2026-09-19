"""Committed CRM worlds for races that `db_session` cannot express.

`db_session` runs a test inside one outer transaction, which is perfect
isolation and cannot represent two colleagues committing independently: a race
written inside one transaction is two sequential calls wearing a costume. The
tests that prove the CRM's transitions under concurrency (CRM-02..CRM-11) run
two real sessions on two real connections instead, and use `lock_waiter` to
prove the second was genuinely blocked behind the first rather than merely
later.

Nothing here is rolled back for us, so every world is removed in the fixture's
teardown: its audit rows (whose tenant is `SET NULL`), its tenant (which
cascades to everything tenant-scoped) and its users.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.conversation import Contact, Conversation, ConversationMode
from app.db.models.enums import TenantStatus
from app.db.models.follow_up import FollowUp
from app.db.models.lead import Lead, LeadActivity, LeadSource, LeadStatus
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.session import Database
from tests.integration.ai_harness import settings_for, wait_for_lock_waiter

__all__ = ["CrmWorld", "World", "crm"]


@dataclass
class World:
    """One workspace with three colleagues and two customers.

    `conversation` and `lead` are customer X's; `other_conversation` is
    customer Y's, for the same-workspace wrong-customer cases (CRM-14).
    """

    tenant_id: uuid.UUID
    owner: User
    alice: User
    bob: User
    contact_id: uuid.UUID
    other_contact_id: uuid.UUID
    conversation_id: uuid.UUID
    other_conversation_id: uuid.UUID
    lead_id: uuid.UUID


@dataclass
class CrmWorld:
    """Seeds committed worlds, hands out independent sessions, cleans up."""

    database: Database
    tenants: list[uuid.UUID] = field(default_factory=list)
    users: list[uuid.UUID] = field(default_factory=list)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session on its own connection that the caller commits explicitly."""
        async with self.database.session_factory() as session:
            yield session

    async def world(self, *, mode: ConversationMode = ConversationMode.AI) -> World:
        tag = uuid.uuid4().hex[:10]
        async with self.database.session() as session:
            tenant = Tenant(name="CRM", slug=f"crm-{tag}", status=TenantStatus.ACTIVE)
            session.add(tenant)
            owner = _user(f"owner-{tag}")
            alice = _user(f"alice-{tag}")
            bob = _user(f"bob-{tag}")
            session.add_all([owner, alice, bob])
            await session.flush()
            session.add_all(
                [
                    Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER),
                    Membership(tenant_id=tenant.id, user_id=alice.id, role=TenantRole.MEMBER),
                    Membership(tenant_id=tenant.id, user_id=bob.id, role=TenantRole.MEMBER),
                ]
            )
            account = WhatsAppAccount(
                tenant_id=tenant.id,
                phone_number_id=f"PN-{tag}",
                waba_id="waba-crm",
                display_phone_number="+20 100 000 0009",
                status=WhatsAppAccountStatus.ACTIVE,
                ownership_started_at=datetime.now(UTC) - timedelta(days=2),
                ownership_verified_at=datetime.now(UTC) - timedelta(days=2),
            )
            customer_x = Contact(tenant_id=tenant.id, wa_id=f"2011{tag[:8]}")
            customer_y = Contact(tenant_id=tenant.id, wa_id=f"2012{tag[:8]}")
            session.add_all([account, customer_x, customer_y])
            await session.flush()
            conversation = Conversation(
                tenant_id=tenant.id,
                contact_id=customer_x.id,
                account_id=account.id,
                mode=mode,
                last_inbound_at=datetime.now(UTC) - timedelta(minutes=10),
            )
            other = Conversation(
                tenant_id=tenant.id,
                contact_id=customer_y.id,
                account_id=account.id,
                last_inbound_at=datetime.now(UTC) - timedelta(minutes=10),
            )
            session.add_all([conversation, other])
            await session.flush()
            lead = Lead(
                tenant_id=tenant.id,
                contact_id=customer_x.id,
                conversation_id=conversation.id,
                status=LeadStatus.NEW,
                source=LeadSource.AGENT,
                name="Ahmed",
                email="old.address@example.com",
            )
            session.add(lead)
            await session.flush()
            world = World(
                tenant_id=tenant.id,
                owner=owner,
                alice=alice,
                bob=bob,
                contact_id=customer_x.id,
                other_contact_id=customer_y.id,
                conversation_id=conversation.id,
                other_conversation_id=other.id,
                lead_id=lead.id,
            )
        self.tenants.append(world.tenant_id)
        self.users.extend([owner.id, alice.id, bob.id])
        return world

    async def lock_waiter(self) -> None:
        await wait_for_lock_waiter(self.database.engine)

    # ------------------------------------------------------------- reading

    async def conversation(self, world: World) -> Conversation:
        async with self.session() as session:
            found = await session.get(Conversation, world.conversation_id)
            assert found is not None
            return found

    async def lead(self, world: World) -> Lead:
        async with self.session() as session:
            found = await session.get(Lead, world.lead_id)
            assert found is not None
            return found

    async def follow_up(self, follow_up_id: uuid.UUID) -> FollowUp:
        async with self.session() as session:
            found = await session.get(FollowUp, follow_up_id)
            assert found is not None
            return found

    async def audit(self, world: World, *actions: AuditAction) -> list[AuditLog]:
        async with self.session() as session:
            statement = select(AuditLog).where(AuditLog.tenant_id == world.tenant_id)
            if actions:
                statement = statement.where(AuditLog.action.in_(actions))
            return list(await session.scalars(statement.order_by(AuditLog.occurred_at)))

    async def handoffs(self, world: World) -> int:
        async with self.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(AnalyticsEvent)
                    .where(
                        AnalyticsEvent.tenant_id == world.tenant_id,
                        AnalyticsEvent.event_type == AnalyticsEventType.HANDOFF,
                    )
                )
                or 0
            )

    async def timeline(self, world: World) -> list[LeadActivity]:
        """The lead's activity oldest first, in the order the API pages it."""
        async with self.session() as session:
            return list(
                await session.scalars(
                    select(LeadActivity)
                    .where(LeadActivity.lead_id == world.lead_id)
                    .order_by(LeadActivity.created_at, LeadActivity.id)
                )
            )

    async def execute(self, statement: Any) -> None:
        async with self.database.session() as session:
            await session.execute(statement)

    async def cleanup(self) -> None:
        async with self.database.session() as session:
            if self.tenants:
                await session.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(self.tenants)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(self.tenants)))
            if self.users:
                await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(self.users)))
                await session.execute(delete(User).where(User.id.in_(self.users)))


def _user(handle: str) -> User:
    return User(email=f"{handle}@crm.test", hashed_password="x", full_name=handle.split("-")[0])


@pytest_asyncio.fixture
async def crm(prepared_database: str) -> AsyncIterator[CrmWorld]:
    database = Database(settings_for(prepared_database))
    world = CrmWorld(database=database)
    try:
        yield world
    finally:
        try:
            await world.cleanup()
        finally:
            await database.dispose()
