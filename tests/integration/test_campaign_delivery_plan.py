"""The plan, the deadline and the ETA, over the real API and workers (LIM-05, LIM-06, PG-13)."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.timezones import zone
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.enums import TenantRole
from app.db.session import Database
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import (
    FakeMeta,
    SimClock,
    compose,
    erase,
    quiesce,
    read_limit,
    seed,
    worker,
)

# Quiet hours are real only in the tests that choose their own moments
# (`tomorrow_cairo`): a test on the real clock, run at 22:30 Cairo, would find
# every campaign waiting for the morning (CPF-02).
pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit]


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="plan")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


def tomorrow_cairo(hour: int, minute: int = 0) -> datetime:
    today = datetime.now(UTC).astimezone(zone("Africa/Cairo")).date()
    return datetime(
        today.year, today.month, today.day, hour, minute, tzinfo=zone("Africa/Cairo")
    ) + timedelta(days=1)


@pytest.mark.quiet_hours
async def test_scheduling_answers_with_the_plan_and_a_late_start_waits_for_nine(
    database: Database,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    workspace = await seed(database, recipients=300)
    await read_limit(database, meta, workspace.account_id)
    campaign_id = await compose(database, workspace, schedule=False)
    start = tomorrow_cairo(23)

    async with client_for(database, workspace) as client:
        response = await client.post(
            f"{API}/campaigns/{campaign_id}/schedule",
            json={"scheduled_at": start.isoformat()},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    plan = body["delivery_plan"]
    assert plan["timezone"] == "Africa/Cairo"
    first = datetime.fromisoformat(plan["first_send_at"])
    assert first == (start + timedelta(days=1)).replace(hour=9)
    assert plan["first_send_at_local"].endswith(("+02:00", "+03:00"))
    assert "starts_after_quiet_hours" in plan["notices"]
    assert "spreads_over_days" in plan["notices"]
    assert [day["recipients"] for day in plan["days"]] == [200, 100]
    assert plan["recipients_planned"] == 300
    assert body["eta"]["estimated_completion_at"] == plan["estimated_completion_at"]


async def test_the_plan_is_an_admin_s_and_recomputed_on_demand(database: Database) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    workspace = await seed(database, recipients=10)
    await read_limit(database, meta, workspace.account_id)
    campaign_id = await compose(database, workspace, schedule=False)

    async with client_for(database, workspace) as admin:
        response = await admin.get(f"{API}/campaigns/{campaign_id}/delivery-plan")
    assert response.status_code == 200, response.text
    assert response.json()["recipients_planned"] == 10
    async with client_for(database, workspace, role=TenantRole.MEMBER) as member:
        assert (await member.get(f"{API}/campaigns/{campaign_id}/delivery-plan")).status_code == 403


@pytest.mark.quiet_hours
async def test_a_deadline_needs_its_offset_and_must_follow_the_start(database: Database) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    workspace = await seed(database, recipients=3)
    await read_limit(database, meta, workspace.account_id)
    campaign_id = await compose(database, workspace, schedule=False)
    start = tomorrow_cairo(12)

    async with client_for(database, workspace) as client:
        naive = await client.post(
            f"{API}/campaigns/{campaign_id}/schedule",
            json={"scheduled_at": start.isoformat(), "send_until": "2030-01-01T10:00:00"},
        )
        early = await client.post(
            f"{API}/campaigns/{campaign_id}/schedule",
            json={
                "scheduled_at": start.isoformat(),
                "send_until": (start - timedelta(hours=1)).isoformat(),
            },
        )
        accepted = await client.post(
            f"{API}/campaigns/{campaign_id}/schedule",
            json={
                "scheduled_at": start.isoformat(),
                "send_until": (start + timedelta(days=2)).isoformat(),
            },
        )
    assert naive.status_code == 422
    assert naive.json()["error"]["code"] == "send_until_needs_offset"
    assert early.status_code == 422
    assert early.json()["error"]["code"] == "send_until_before_start"
    assert accepted.status_code == 200, accepted.text
    assert datetime.fromisoformat(accepted.json()["send_until"]) == start + timedelta(days=2)
    assert accepted.json()["send_until_local"] is not None


async def test_people_still_pending_at_the_deadline_are_skipped_and_the_campaign_completes(
    prepared_database: str,
) -> None:
    """send_until passes with 50 pending (250 people, 200 a day): 50 skipped, completed."""
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="deadline")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=250)
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await compose(database, workspace, schedule=False)
        from app.services.campaign_service import CampaignService

        async with database.session_factory() as session:
            await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
                campaign_id=campaign_id, send_until=datetime.now(UTC) + timedelta(hours=2)
            )
            await session.commit()
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        for _ in range(40):
            await clock.sweep(replica)
            async with database.session_factory() as session:
                row = await session.get(Campaign, campaign_id)
                assert row is not None
                if row.status is CampaignStatus.COMPLETED:
                    break
                clock.now = max(row.next_send_at or clock.now, clock.now + timedelta(seconds=1))

        async with database.session_factory() as session:
            row = await session.get(Campaign, campaign_id)
            assert row is not None
            assert row.status is CampaignStatus.COMPLETED
            outcomes = Counter(
                (status.value, reason)
                for status, reason in (
                    await session.execute(
                        select(CampaignRecipient.status, CampaignRecipient.reason_code).where(
                            CampaignRecipient.campaign_id == campaign_id
                        )
                    )
                ).all()
            )
        assert outcomes == Counter({("sent", None): 200, ("skipped", "send_window_closed"): 50})
        assert len(meta.requests) == 200
        assert max(entry["at"] for entry in meta.requests) < row.send_until
    finally:
        await erase(database)
        await database.dispose()


async def test_the_eta_of_a_live_campaign_and_none_for_a_paused_one(database: Database) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    workspace = await seed(database, recipients=120)
    await read_limit(database, meta, workspace.account_id)
    campaign_id = await compose(database, workspace, rate=60)

    async with client_for(database, workspace) as client:
        live = (await client.get(f"{API}/campaigns/{campaign_id}")).json()
        listed = (await client.get(f"{API}/campaigns")).json()["items"]
        await client.post(f"{API}/campaigns/{campaign_id}/pause")
        paused = (await client.get(f"{API}/campaigns/{campaign_id}")).json()

    eta = live["eta"]
    assert eta["reason"] is None
    completion = datetime.fromisoformat(eta["estimated_completion_at"])
    assert timedelta(minutes=1) < completion - datetime.now(UTC) < timedelta(minutes=3)
    assert eta["basis"] == ["rate"] and eta["confidence"] == "high"
    assert eta["estimated_completion_at_local"] is not None
    assert next(item for item in listed if item["id"] == str(campaign_id))["eta"] is not None
    assert paused["eta"]["estimated_completion_at"] is None
    assert paused["eta"]["reason"] == "paused"
