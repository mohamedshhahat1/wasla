"""A workspace that is not served does not broadcast (CMP-02, CMQ-11).

Ported from the audit's P03 (suspend, delete) and P12 (dunning) probes: on the
audited head a suspended, soft-deleted or unpaid workspace's scheduled
campaign sent 10/10. The workspace is suspended and deleted here through
`WorkspaceService`, exactly as the platform and the owner do it - never by
setting a column - and "sent" is counted at the Meta transport.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

import app.services.workspace_serving as serving_module
from app.core.config import Settings
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.billing import Subscription, SubscriptionStatus
from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import Database
from app.services.campaign_service import CampaignService
from app.services.follow_up_service import FollowUpService
from app.services.workspace_service import WorkspaceService
from app.services.workspace_serving import WorkspaceServing
from tests.integration.campaign_harness import (
    USER_DOMAIN,
    FakeMeta,
    Workspace,
    compose,
    erase,
    ledger,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


async def _staff(database: Database) -> uuid.UUID:
    async with database.session_factory() as session:
        user = User(
            email=f"staff-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            hashed_password="x",
            is_active=True,
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _owner(database: Database, workspace: Workspace) -> uuid.UUID:
    async with database.session_factory() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            hashed_password="x",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                tenant_id=workspace.tenant_id,
                user_id=user.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.ACTIVE,
            )
        )
        await session.commit()
        return user.id


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test")


async def _suspend(database: Database, workspace: Workspace, staff_id: uuid.UUID) -> None:
    async with database.session_factory() as session:
        actor = await session.get(User, staff_id)
        assert actor is not None
        await WorkspaceService(session, settings=_settings()).suspend(
            tenant_id=workspace.tenant_id, actor=actor, reason="Harness suspension."
        )
        await session.commit()


async def _restore(database: Database, workspace: Workspace, staff_id: uuid.UUID) -> None:
    async with database.session_factory() as session:
        actor = await session.get(User, staff_id)
        assert actor is not None
        await WorkspaceService(session, settings=_settings()).restore(
            tenant_id=workspace.tenant_id, actor=actor
        )
        await session.commit()


async def _delete(database: Database, workspace: Workspace, owner_id: uuid.UUID) -> None:
    async with database.session_factory() as session:
        actor = await session.get(User, owner_id)
        tenant = await session.get(Tenant, workspace.tenant_id)
        assert actor is not None and tenant is not None
        await WorkspaceService(session, settings=_settings()).delete(
            tenant_id=workspace.tenant_id, actor=actor, confirmation=tenant.slug
        )
        await session.commit()


async def _campaign(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


async def _paused_audits(database: Database, campaign_id: uuid.UUID) -> list[AuditLog]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.CAMPAIGN_PAUSED,
                        AuditLog.target_id == campaign_id,
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest_asyncio.fixture
async def world(prepared_database: str) -> Any:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="w")
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def test_a_suspended_workspace_sends_nothing_and_its_campaign_is_paused(world: Any) -> None:
    """P03 `suspend`: audit 10/10 sent. Suspended through `WorkspaceService.suspend`."""
    meta, each, database = world
    workspace = await seed(database, recipients=10)
    campaign_id = await compose(database, workspace)
    staff = await _staff(database)

    await _suspend(database, workspace, staff)
    await each.run_once()

    assert len(meta.requests) == 0
    row = await _campaign(database, campaign_id)
    assert row.status is CampaignStatus.PAUSED
    assert row.status_reason == CampaignStopReason.WORKSPACE_SUSPENDED
    # One entry, written by the suspension, naming the staff member who acted.
    audits = await _paused_audits(database, campaign_id)
    assert [(entry.actor_id, entry.actor_kind) for entry in audits] == [
        (staff, AuditActorKind.PLATFORM_STAFF)
    ]
    assert audits[0].meta == {"reason": "workspace_suspended"}


async def test_a_suspension_mid_batch_stops_at_the_next_recipient(world: Any) -> None:
    """Suspended after Meta has seen 3 of 30 sends: at most the one in flight follows."""
    meta, each, database = world
    workspace = await seed(database, recipients=30)
    campaign_id = await compose(database, workspace)
    staff = await _staff(database)

    async def on_request(payload: dict[str, object], n: int) -> None:
        if n == 3:
            await _suspend(database, workspace, staff)

    meta.on_request = on_request
    await each.run_once()

    assert len(meta.requests) <= 4
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.PAUSED
    assert row.meters == row.sent == len(meta.requests)
    assert (await _campaign(database, campaign_id)).status_reason == "workspace_suspended"


async def test_a_deleted_workspace_sends_nothing(world: Any) -> None:
    """P03 `delete`: audit 10/10 sent. Deleted through `WorkspaceService.delete`."""
    meta, each, database = world
    workspace = await seed(database, recipients=10)
    campaign_id = await compose(database, workspace)
    owner = await _owner(database, workspace)

    await _delete(database, workspace, owner)
    await each.run_once()

    assert len(meta.requests) == 0
    row = await _campaign(database, campaign_id)
    assert row.status is CampaignStatus.PAUSED
    assert row.status_reason == CampaignStopReason.WORKSPACE_DELETED
    audits = await _paused_audits(database, campaign_id)
    assert [(entry.actor_id, entry.actor_kind) for entry in audits] == [
        (owner, AuditActorKind.USER)
    ]


async def test_the_worker_gate_catches_what_the_transition_did_not(world: Any) -> None:
    """Suspended by a path that paused nothing: the per-batch gate is authoritative."""
    meta, each, database = world
    workspace = await seed(database, recipients=5)
    campaign_id = await compose(database, workspace)
    async with database.session_factory() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == workspace.tenant_id).values(status="suspended")
        )
        await session.commit()

    await each.run_once()

    assert len(meta.requests) == 0
    row = await _campaign(database, campaign_id)
    assert row.status is CampaignStatus.PAUSED
    assert row.status_reason == CampaignStopReason.WORKSPACE_SUSPENDED
    audits = await _paused_audits(database, campaign_id)
    assert [entry.actor_kind for entry in audits] == [AuditActorKind.SYSTEM]


async def test_a_restored_workspace_stays_paused_until_an_admin_reschedules(world: Any) -> None:
    """No automatic resume; a reschedule sends the remaining people only, nobody twice."""
    meta, each, database = world
    workspace = await seed(database, recipients=20)
    campaign_id = await compose(database, workspace)
    staff = await _staff(database)
    await _owner(database, workspace)  # a restore needs one

    async def on_request(payload: dict[str, object], n: int) -> None:
        if n == 5:
            await _suspend(database, workspace, staff)

    meta.on_request = on_request
    await each.run_once()
    meta.on_request = None
    before = len(meta.requests)

    await _restore(database, workspace, staff)
    await each.run_once()
    assert len(meta.requests) == before, "a restore must not resume a campaign by itself"
    assert (await _campaign(database, campaign_id)).status is CampaignStatus.PAUSED

    async with database.session_factory() as session:
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id
        )
        await session.commit()
    for _ in range(3):
        await each.run_once()
        await asyncio.sleep(0.05)

    assert meta.received_twice() == 0
    assert len(meta.requests) == 20
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.COMPLETED
    assert row.sent == row.meters == 20
    assert (await _campaign(database, campaign_id)).status_reason is None


@pytest.mark.parametrize(
    ("subscription", "sends", "reason"),
    [
        (SubscriptionStatus.SUSPENDED, 0, CampaignStopReason.SUBSCRIPTION_SUSPENDED),
        (SubscriptionStatus.PAST_DUE, 10, None),
    ],
)
async def test_dunning_suspension_pauses_and_the_grace_keeps_sending(
    world: Any,
    subscription: SubscriptionStatus,
    sends: int,
    reason: CampaignStopReason | None,
) -> None:
    """P12 (CMQ-11): audit 10/10 for both. Suspended stops; `past_due` is the grace."""
    meta, each, database = world
    workspace = await seed(database, recipients=10)
    campaign_id = await compose(database, workspace)
    async with database.session_factory() as session:
        await session.execute(
            update(Subscription)
            .where(Subscription.tenant_id == workspace.tenant_id)
            .values(status=subscription)
        )
        await session.commit()

    await each.run_once()
    await each.run_once()

    assert len(meta.requests) == sends
    row = await _campaign(database, campaign_id)
    assert row.status_reason == reason
    assert row.status is (CampaignStatus.PAUSED if reason else CampaignStatus.COMPLETED)


async def test_follow_ups_and_campaigns_ask_the_same_predicate(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One definition of "served" (CMP-02): both gates call `workspace_serving`."""
    _, _, database = world
    workspace = await seed(database, recipients=1)
    calls: list[uuid.UUID] = []

    async def suspended(session: object, tenant_id: uuid.UUID) -> WorkspaceServing:
        calls.append(tenant_id)
        return WorkspaceServing.SUSPENDED

    monkeypatch.setattr(serving_module, "workspace_serving", suspended)
    async with database.session_factory() as session:
        follow_ups = FollowUpService(session=session, tenant_id=workspace.tenant_id)
        campaigns = CampaignService(session=session, tenant_id=workspace.tenant_id)
        assert await follow_ups._workspace_is_served() is False
        assert await campaigns._unserved_reason() is CampaignStopReason.WORKSPACE_SUSPENDED

    assert calls == [workspace.tenant_id, workspace.tenant_id]
