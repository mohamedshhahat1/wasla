"""Nobody receives a campaign at night in the workspace's own time (TZ-03, TZ-04).

Real `CampaignWorker`s with their own databases and commits; the fake Meta
stamps every request with the worker's clock (`SimClock`), and "never inside
quiet hours" is checked against those stamps at the transport, in the
workspace's zone. Time moves in the worker's clock only: each sweep is started
at a moment the test chooses, and advances as the sweep runs.

Africa/Cairo observes daylight saving (UTC+2 in winter, UTC+3 in summer): the
window's end is computed in local time, so a fixed offset is an hour wrong on
one side of every transition - which the April and October tests catch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.timezones import zone
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.enums import TenantRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import Database
from app.services.campaign_service import CampaignService
from app.services.campaign_settings_service import CampaignSettingsService
from app.services.quiet_hours import QuietHours
from app.services.workspace_service import WorkspaceService
from tests.integration.campaign_harness import (
    FakeMeta,
    SimClock,
    compose,
    erase,
    make_due,
    quiesce,
    seed,
    worker,
)

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

CAIRO = "Africa/Cairo"
DUBAI = "Asia/Dubai"
DEFAULT_WINDOW = QuietHours(enabled=True, start=time(22), end=time(9), timezone=CAIRO)


def local(at: datetime, name: str = CAIRO) -> datetime:
    return at.astimezone(zone(name))


def cairo(
    year: int, month: int, day: int, hour: int, minute: int = 0, second: float = 0
) -> datetime:
    """A Cairo wall-clock moment as an aware datetime (fold 0)."""
    whole = int(second)
    micro = round((second - whole) * 1_000_000)
    return datetime(year, month, day, hour, minute, whole, micro, tzinfo=zone(CAIRO))


def sends_inside_quiet_hours(meta: FakeMeta, window: QuietHours = DEFAULT_WINDOW) -> int:
    return sum(1 for entry in meta.requests if window.contains(entry["at"]))


async def campaign_row(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.scalar(select(Campaign).where(Campaign.id == campaign_id))
        assert row is not None
        return row


async def pending_attempts(database: Database, campaign_id: uuid.UUID) -> list[int]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(CampaignRecipient.attempts).where(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.status == RecipientStatus.PENDING,
                    )
                )
            ).scalars()
        )


async def audit_waits(database: Database, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(AuditLog.meta).where(
                    AuditLog.action == AuditAction.CAMPAIGN_WAITING,
                    AuditLog.target_id == campaign_id,
                )
            )
        ).scalars()
        return [dict(meta or {}) for meta in rows]


async def _drain(clock: SimClock, replica: Any, *, rounds: int = 20) -> None:
    for _ in range(rounds):
        if await clock.sweep(replica) == 0:
            return


# --------------------------------------------------------------------------- the edge


async def test_a_batch_reaching_the_window_stops_at_the_next_person_and_resumes_in_the_morning(
    prepared_database: str,
) -> None:
    """21:59:59 local, Meta answering in 0.3 s: the batch crosses 22:00 and stops there.

    Nobody is sent between 22:00 and 09:00 local; the rest go at 09:00; no
    attempt is spent and the status never leaves `running`.
    """
    clock = SimClock(now=cairo(2026, 10, 20, 21, 59, 59.0))
    meta = FakeMeta(latency=0.3)
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-edge")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=20)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=clock.now)

        await clock.sweep(replica)

        assert 1 <= len(meta.requests) < 20
        assert sends_inside_quiet_hours(meta) == 0
        row = await campaign_row(db, campaign_id)
        assert row.status is CampaignStatus.RUNNING
        assert row.waiting_reason == "quiet_hours"
        assert row.resumes_at == cairo(2026, 10, 21, 9)
        assert row.lease_token is None
        assert set(await pending_attempts(db, campaign_id)) == {0}
        sent_before_night = len(meta.requests)

        clock.now = cairo(2026, 10, 20, 23, 30)
        await _drain(clock, replica)
        clock.now = cairo(2026, 10, 21, 8, 59)
        await _drain(clock, replica)
        assert len(meta.requests) == sent_before_night

        clock.now = cairo(2026, 10, 21, 9, 0, 1)
        meta.latency = 0.0
        await _drain(clock, replica)

        assert len(meta.requests) == 20
        assert meta.received_twice() == 0
        assert sends_inside_quiet_hours(meta) == 0
        row = await campaign_row(db, campaign_id)
        assert row.status is CampaignStatus.COMPLETED
        assert row.waiting_reason is None and row.resumes_at is None
        waits = await audit_waits(db, campaign_id)
        assert [entry["reason"] for entry in waits] == ["quiet_hours"]
    finally:
        await erase(db)
        await db.dispose()


async def test_a_schedule_inside_the_window_is_accepted_and_starts_after_it(
    prepared_database: str,
) -> None:
    """TZ-04: scheduled for 23:00 local; the first send is at 09:00, and it stays scheduled."""
    clock = SimClock(now=cairo(2026, 10, 22, 23))
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-schedule")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=5)
        campaign_id = await compose(db, workspace, schedule=False)
        async with db.session_factory() as session:
            scheduled = await CampaignService(
                session=session, tenant_id=workspace.tenant_id
            ).schedule(campaign_id=campaign_id, scheduled_at=cairo(2026, 10, 22, 23))
            assert scheduled.status is CampaignStatus.SCHEDULED
            await session.commit()

        await _drain(clock, replica)
        row = await campaign_row(db, campaign_id)
        assert meta.requests == []
        assert row.status is CampaignStatus.SCHEDULED
        assert row.started_at is None
        assert row.waiting_reason == "quiet_hours"
        assert row.resumes_at == cairo(2026, 10, 23, 9)

        clock.now = cairo(2026, 10, 23, 9, 0, 5)
        await _drain(clock, replica)
        assert len(meta.requests) == 5
        assert min(entry["at"] for entry in meta.requests) >= cairo(2026, 10, 23, 9)
        assert (await campaign_row(db, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(db)
        await db.dispose()


# --------------------------------------------------------------------------- DST


async def test_the_window_ends_at_nine_local_across_the_april_transition(
    prepared_database: str,
) -> None:
    """Cairo moves from UTC+2 to UTC+3 at 00:00 on 2026-04-24.

    At 23:30 on the 23rd (+02:00) the window ends at 09:00 on the 24th, which is
    06:00 UTC (+03:00). A fixed +02:00 would say 07:00 UTC.
    """
    before = cairo(2026, 4, 23, 23, 30)
    after = cairo(2026, 4, 24, 9)
    assert before.utcoffset() == timedelta(hours=2)
    assert after.utcoffset() == timedelta(hours=3)

    clock = SimClock(now=before)
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-april")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=3)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=before)

        await _drain(clock, replica)
        row = await campaign_row(db, campaign_id)
        assert meta.requests == []
        assert row.resumes_at == datetime(2026, 4, 24, 6, 0, tzinfo=UTC)

        clock.now = datetime(2026, 4, 24, 6, 0, 30, tzinfo=UTC)
        await _drain(clock, replica)
        assert len(meta.requests) == 3
        assert sends_inside_quiet_hours(meta) == 0
    finally:
        await erase(db)
        await db.dispose()


async def test_the_window_ends_at_nine_local_across_the_october_transition(
    prepared_database: str,
) -> None:
    """Cairo moves from UTC+3 to UTC+2 at the end of 2026-10-29.

    At 22:30 on the 29th (+03:00) the window ends at 09:00 on the 30th, which is
    07:00 UTC (+02:00). A fixed +03:00 would say 06:00 UTC - and even made due
    at 06:30 UTC (08:30 local) the worker sends nothing.
    """
    before = cairo(2026, 10, 29, 22, 30)
    after = cairo(2026, 10, 30, 9)
    assert before.utcoffset() == timedelta(hours=3)
    assert after.utcoffset() == timedelta(hours=2)

    clock = SimClock(now=before)
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-october")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=3)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=before)

        await _drain(clock, replica)
        assert (await campaign_row(db, campaign_id)).resumes_at == datetime(
            2026, 10, 30, 7, 0, tzinfo=UTC
        )

        early = datetime(2026, 10, 30, 6, 30, tzinfo=UTC)
        await make_due(db, campaign_id, at=early)
        clock.now = early
        await _drain(clock, replica)
        assert meta.requests == []
        assert (await campaign_row(db, campaign_id)).resumes_at == datetime(
            2026, 10, 30, 7, 0, tzinfo=UTC
        )

        clock.now = datetime(2026, 10, 30, 7, 0, 30, tzinfo=UTC)
        await _drain(clock, replica)
        assert len(meta.requests) == 3
        assert sends_inside_quiet_hours(meta) == 0
    finally:
        await erase(db)
        await db.dispose()


# --------------------------------------------------------------------------- zones


async def test_two_workspaces_obey_each_their_own_zone(prepared_database: str) -> None:
    """05:30 UTC: 08:30 in Cairo (quiet) and 09:30 in Dubai (not): Dubai sends, Cairo waits."""
    moment = datetime(2026, 10, 20, 5, 30, tzinfo=UTC)
    clock = SimClock(now=moment)
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-zones")
    try:
        await quiesce(db)
        in_cairo = await seed(db, recipients=4, timezone=CAIRO)
        in_dubai = await seed(db, recipients=4, timezone=DUBAI)
        cairo_campaign = await compose(db, in_cairo)
        dubai_campaign = await compose(db, in_dubai)
        await make_due(db, cairo_campaign, at=moment)
        await make_due(db, dubai_campaign, at=moment)

        await _drain(clock, replica)

        assert (await campaign_row(db, dubai_campaign)).status is CampaignStatus.COMPLETED
        cairo_row = await campaign_row(db, cairo_campaign)
        assert cairo_row.waiting_reason == "quiet_hours"
        assert cairo_row.resumes_at == datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
        assert len(meta.requests) == 4
        dubai_window = QuietHours(enabled=True, start=time(22), end=time(9), timezone=DUBAI)
        assert sends_inside_quiet_hours(meta, dubai_window) == 0
    finally:
        await erase(db)
        await db.dispose()


# --------------------------------------------------------------------------- changes


async def _owner(db: Database, tenant_id: uuid.UUID) -> User:
    async with db.session_factory() as session:
        user = User(
            email=f"quiet-{uuid.uuid4().hex[:8]}@harness.example.com",
            full_name="Quiet",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()
        return user


async def test_an_edited_window_recomputes_the_wait_and_switching_off_releases_it(
    prepared_database: str,
) -> None:
    moment = cairo(2026, 10, 21, 6, 30)
    clock = SimClock(now=moment)
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-edit")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=3)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=moment)
        await _drain(clock, replica)
        assert (await campaign_row(db, campaign_id)).resumes_at == cairo(2026, 10, 21, 9)
        actor = await _owner(db, workspace.tenant_id)

        async with db.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            await CampaignSettingsService(session, tenant=tenant).update(
                actor=actor,
                actor_role=TenantRole.TENANT_OWNER,
                quiet_hours_enabled=True,
                quiet_hours_start=time(22),
                quiet_hours_end=time(7),
            )
            await session.commit()
        await _drain(clock, replica)
        row = await campaign_row(db, campaign_id)
        assert meta.requests == []
        assert row.resumes_at == cairo(2026, 10, 21, 7)

        async with db.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            await CampaignSettingsService(session, tenant=tenant).update(
                actor=actor,
                actor_role=TenantRole.TENANT_OWNER,
                quiet_hours_enabled=False,
                quiet_hours_start=time(22),
                quiet_hours_end=time(7),
            )
            await session.commit()
        clock.now = cairo(2026, 10, 21, 6, 35)
        await _drain(clock, replica)
        assert len(meta.requests) == 3
        assert (await campaign_row(db, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(db)
        await db.dispose()


async def test_a_new_zone_moves_the_window_and_never_the_scheduled_time(
    prepared_database: str,
) -> None:
    moment = datetime(2026, 10, 20, 5, 30, tzinfo=UTC)
    clock = SimClock(now=moment)
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="quiet-zone-change")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=2, timezone=CAIRO)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=moment)
        await _drain(clock, replica)
        assert (await campaign_row(db, campaign_id)).waiting_reason == "quiet_hours"
        scheduled_at = (await campaign_row(db, campaign_id)).scheduled_at
        actor = await _owner(db, workspace.tenant_id)

        async with db.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            await WorkspaceService(session).update(
                tenant=tenant,
                actor=actor,
                actor_role=TenantRole.TENANT_OWNER,
                timezone=DUBAI,
            )
            await session.commit()
        assert (await campaign_row(db, campaign_id)).scheduled_at == scheduled_at

        await _drain(clock, replica)
        assert len(meta.requests) == 2
        assert (await campaign_row(db, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(db)
        await db.dispose()
