"""The campaign allowance is reserved under its lock, from counts read under it (CMP-03/04/24).

Ported from the audit's P02 (concurrent scheduling, gated and natural), P02b
(sequential control) and P11b (materialisation racing scheduling). On the
audited head two campaigns of 20 scheduled at the same moment against an
allowance of 30 were both accepted in 40 of 40 trials, and a wider audience
landed on an already-scheduled campaign: 200 pending against 10 reserved.

Every schedule here goes through `CampaignService.schedule` with the real
`EntitlementService` and its own session and commit, as two admins' requests
would.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.core.exceptions import PlanLimitExceededError, ValidationError
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.channel import Channel
from app.db.session import Database
from app.repositories.campaign_repository import (
    AudienceFilter,
    AudienceRepository,
    CampaignRecipientRepository,
)
from app.services.campaign_service import CHANNEL_NOT_IN_PLAN_CODE, CampaignService
from app.services.entitlement_service import EntitlementService
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

ALLOWANCE = 30
EACH = 20


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="reservation")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


async def _schedule(database: Database, workspace: Workspace, campaign_id: uuid.UUID) -> str:
    async with database.session_factory() as session:
        service = CampaignService(
            session=session,
            tenant_id=workspace.tenant_id,
            entitlements=EntitlementService(session, tenant_id=workspace.tenant_id),
        )
        try:
            await service.schedule(campaign_id=campaign_id)
        except PlanLimitExceededError as error:
            await session.rollback()
            return error.error_code
        except ValidationError:
            await session.rollback()
            return "refused"
        await session.commit()
        return "scheduled"


async def _promised(database: Database, workspace: Workspace) -> int:
    """Pending recipients of this workspace's scheduled and running campaigns."""
    async with database.session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CampaignRecipient)
                .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
                .where(
                    Campaign.tenant_id == workspace.tenant_id,
                    Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
                    CampaignRecipient.status == RecipientStatus.PENDING,
                )
            )
            or 0
        )


async def _drafts(database: Database, workspace: Workspace, count: int) -> list[uuid.UUID]:
    return [await compose(database, workspace, schedule=False) for _ in range(count)]


async def test_a_second_campaign_past_the_remainder_is_refused(database: Database) -> None:
    """P02b, the sequential control: 20 then 20 against 30 - the second is refused 402.

    Kills M21 (the reservation ignoring other live campaigns): without the
    count of what is already promised, 20 fits in 30 twice.
    """
    workspace = await seed(
        database, recipients=EACH, limits={"period_campaign_messages": ALLOWANCE}
    )
    first, second = await _drafts(database, workspace, 2)

    assert await _schedule(database, workspace, first) == "scheduled"
    assert await _schedule(database, workspace, second) == "plan_limit_exceeded"
    assert await _promised(database, workspace) == EACH


async def test_a_campaign_larger_than_the_allowance_is_refused(database: Database) -> None:
    """Kills M20: the reservation not taken at all lets 40 recipients past an allowance of 30."""
    workspace = await seed(database, recipients=40, limits={"period_campaign_messages": ALLOWANCE})
    (campaign,) = await _drafts(database, workspace, 1)

    assert await _schedule(database, workspace, campaign) == "plan_limit_exceeded"
    assert await _promised(database, workspace) == 0


async def test_two_admins_gated_at_the_old_decision_point_cannot_both_fit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02 gated: both schedulers held just after reading what others promised.

    On the audited head that read ran before the lock, so both read 0 and both
    were accepted. Now it runs under the lock: the first holds it at the gate,
    the second waits for the lock rather than reaching the gate, and once the
    first commits it reads 20 and is refused.
    """
    workspace = await seed(
        database, recipients=EACH, limits={"period_campaign_messages": ALLOWANCE}
    )
    first, second = await _drafts(database, workspace, 2)
    barrier = asyncio.Barrier(2)
    original = CampaignRecipientRepository.pending_in_other_live_campaigns

    async def gated(self: CampaignRecipientRepository, campaign_id: uuid.UUID) -> int:
        counted = await original(self, campaign_id)
        # The second scheduler is waiting for the lock, not at this gate, so
        # the barrier is never met; the first goes on once it gives up.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(barrier.wait(), timeout=1.5)
        return counted

    monkeypatch.setattr(CampaignRecipientRepository, "pending_in_other_live_campaigns", gated)
    outcomes = await asyncio.gather(
        _schedule(database, workspace, first), _schedule(database, workspace, second)
    )

    assert sorted(outcomes) == ["plan_limit_exceeded", "scheduled"]
    assert await _promised(database, workspace) == EACH


async def test_two_admins_at_the_same_moment_never_both_fit(database: Database) -> None:
    """P02 natural: 40 ungated trials, exactly one accepted in each (audit: 40/40 both)."""
    workspace = await seed(
        database, recipients=EACH, limits={"period_campaign_messages": ALLOWANCE}
    )
    accepted: list[int] = []
    for _ in range(40):
        first, second = await _drafts(database, workspace, 2)
        outcomes = await asyncio.gather(
            _schedule(database, workspace, first), _schedule(database, workspace, second)
        )
        accepted.append(outcomes.count("scheduled"))
        assert await _promised(database, workspace) <= ALLOWANCE
        async with database.session_factory() as session:
            await session.execute(
                update(Campaign)
                .where(Campaign.id.in_([first, second]))
                .values(status=CampaignStatus.CANCELLED)
            )
            await session.commit()

    assert accepted == [1] * 40


async def test_a_wider_audience_cannot_land_on_a_scheduled_campaign(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P11b: a materialisation racing a schedule never leaves pending above what was reserved.

    One admin widens a draft's audience from 10 to 200 and is held after the
    eligible read; another schedules it meanwhile. On the audited head both
    committed: scheduled, 200 pending, 10 reserved, allowance 50. Now the
    schedule waits for the materialisation's row lock and counts its 200, which
    the allowance refuses.
    """
    workspace = await seed(database, recipients=200, limits={"period_campaign_messages": 50})
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Narrow",
            messages_per_minute=60,
        )
        await service.set_audience(
            campaign_id=campaign.id,
            filters=AudienceFilter(contact_ids=tuple(workspace.contact_ids[:10])),
        )
        await session.commit()
        campaign_id = campaign.id

    reached = asyncio.Event()
    release = asyncio.Event()
    original = AudienceRepository.list_eligible_members

    async def gated(self: AudienceRepository, **kwargs: Any) -> Any:
        members = await original(self, **kwargs)
        reached.set()
        await release.wait()
        return members

    monkeypatch.setattr(AudienceRepository, "list_eligible_members", gated)

    async def widen() -> str:
        async with database.session_factory() as session:
            try:
                await CampaignService(session=session, tenant_id=workspace.tenant_id).set_audience(
                    campaign_id=campaign_id, filters=AudienceFilter()
                )
            except ValidationError:
                await session.rollback()
                return "refused"
            await session.commit()
            return "widened"

    widening = asyncio.create_task(widen())
    await reached.wait()
    scheduling = asyncio.create_task(_schedule(database, workspace, campaign_id))
    await asyncio.sleep(0.5)
    release.set()
    outcomes = {await widening, await scheduling}

    # Whichever order the locks allowed, no scheduled campaign owes more than
    # it reserved: here the widening wins the row and the schedule is refused.
    assert outcomes == {"widened", "plan_limit_exceeded"}
    assert await _promised(database, workspace) <= 50


async def test_a_schedule_after_scheduling_cannot_widen_the_audience(database: Database) -> None:
    """The other order: scheduled first, a later materialisation is refused 422."""
    workspace = await seed(database, recipients=10, limits={"period_campaign_messages": 50})
    campaign_id = await compose(database, workspace, entitlements=True)
    async with database.session_factory() as session:
        with pytest.raises(ValidationError):
            await CampaignService(session=session, tenant_id=workspace.tenant_id).set_audience(
                campaign_id=campaign_id, filters=AudienceFilter()
            )


async def test_a_campaign_on_a_channel_outside_the_plan_is_refused_before_reserving(
    database: Database,
) -> None:
    """CMP-24: accepted and reserved on the audited head, then every copy skipped."""
    workspace = await seed(
        database,
        recipients=5,
        limits={"period_campaign_messages": ALLOWANCE},
        channels=[Channel.INSTAGRAM],
    )
    (campaign,) = await _drafts(database, workspace, 1)

    assert await _schedule(database, workspace, campaign) == CHANNEL_NOT_IN_PLAN_CODE
    assert await _promised(database, workspace) == 0
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign)
        assert row is not None and row.status is CampaignStatus.DRAFT
