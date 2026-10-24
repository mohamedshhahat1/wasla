# ruff: noqa: F811 - the workspace lifecycle fixtures are imported by name.
"""The timezone fixes of the follow-ups stage (TZ-05, TZ-06, TZ-08, TZ-09).

TZ-05: every campaign time a page shows has a `_local` twin in the workspace's
zone. TZ-06: every connection the application opens runs with `TimeZone =
'UTC'`, so a database server set to Africa/Cairo cuts usage days where a UTC
one does. TZ-08: a wall time is resolved by the server - the October fold and
the April gap included - so no browser computes an offset. TZ-09: a campaign
scheduled on the October change day with the right offset starts at 09:00
local; one sent with the stale summer offset keeps the instant it named.

Cairo's changes in the pinned tzdata: the hour 23:00-24:00 on 2026-10-29 is
repeated (+03:00 then +02:00); 00:00-01:00 on 2027-04-30 does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.timezones import zone
from app.db.models.campaign import Campaign, CampaignStatus
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType, unit_for
from app.db.session import Database
from app.repositories.usage_repository import UsageEventRepository
from app.schemas.campaign import CampaignRead
from app.services.quiet_hours import QuietHours
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    FakeMeta,
    SimClock,
    compose,
    erase,
    harness_settings,
    make_due,
    quiesce,
    seed,
    worker,
)
from tests.integration.test_workspace_lifecycle import (  # noqa: F401 - fixtures
    API,
    _login,
    _select_workspace,
    _user,
    _workspace,
    app,
    http,
    lifecycle_settings,
)

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

CAIRO = "Africa/Cairo"
URL = f"{API}/workspace/local-time"
WINDOW = QuietHours(enabled=True, start=time(22), end=time(9), timezone=CAIRO)


def cairo(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=zone(CAIRO))


async def _drain(clock: SimClock, replica: CampaignWorker, *, rounds: int = 20) -> None:
    for _ in range(rounds):
        if await clock.sweep(replica) == 0:
            return


# --------------------------------------------------------------------------- TZ-05


def test_every_campaign_time_has_a_local_twin() -> None:
    """A campaign read in Cairo gives each of its times in Cairo, with the offset of that day."""
    campaign = Campaign(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        template_id=uuid.uuid4(),
        name="Twins",
        status=CampaignStatus.SCHEDULED,
        audience_size=3,
        messages_per_minute=60,
        version=1,
        scheduled_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
        started_at=datetime(2026, 10, 29, 19, 0, tzinfo=UTC),
        completed_at=None,
        cancelled_at=None,
        next_send_at=datetime(2026, 10, 30, 7, 1, tzinfo=UTC),
        created_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 29, 19, 0, tzinfo=UTC),
    )
    read = CampaignRead.from_model(campaign, timezone=CAIRO)

    assert read.scheduled_at_local is not None
    assert read.scheduled_at_local.isoformat() == "2026-10-30T09:00:00+02:00"
    assert read.started_at_local is not None
    assert read.started_at_local.isoformat() == "2026-10-29T22:00:00+03:00"
    assert read.next_send_at_local is not None
    assert read.next_send_at_local.isoformat() == "2026-10-30T09:01:00+02:00"
    assert read.created_at_local is not None
    assert read.created_at_local.isoformat() == "2026-07-01T12:00:00+03:00"
    assert read.updated_at_local is not None
    assert read.completed_at_local is None and read.cancelled_at_local is None

    without_zone = CampaignRead.from_model(campaign)
    assert without_zone.scheduled_at_local is None


# --------------------------------------------------------------------------- TZ-06


@pytest_asyncio.fixture
async def cairo_zoned_database(prepared_database: str) -> AsyncIterator[str]:
    """The test database with its own default `TimeZone` set to Africa/Cairo, then reset.

    On `wasla-fur-pg3` the server itself is already Cairo-zoned; here the
    database default is set so the proof holds on any lane's server.
    """
    admin = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        async with admin.connect() as connection:
            name = await connection.scalar(text("select current_database()"))
            await connection.execute(
                text(f"ALTER DATABASE \"{name}\" SET timezone TO 'Africa/Cairo'")
            )
            await connection.commit()
        yield prepared_database
    finally:
        async with admin.connect() as connection:
            name = await connection.scalar(text("select current_database()"))
            await connection.execute(text(f'ALTER DATABASE "{name}" RESET timezone'))
            await connection.commit()
        await admin.dispose()


async def test_the_application_session_runs_in_utc_on_a_cairo_zoned_server(
    cairo_zoned_database: str,
) -> None:
    """Usage day buckets on a Cairo-zoned server equal a UTC server's (TZ-06).

    Two events at 22:30 UTC on 2026-10-29 and 2026-10-30 - 00:30 the next
    day in Cairo. In UTC they are on the 29th and the 30th. A connection that
    takes the server's zone puts them on the 30th and the 31st.
    """
    raw = create_async_engine(cairo_zoned_database, poolclass=NullPool)
    try:
        async with raw.connect() as connection:
            assert (
                await connection.scalar(text("show timezone")) == CAIRO
            ), "the setup must zone the server, or the proof below is vacuous"
            unpinned = await connection.scalar(
                text("select date_trunc('day', '2026-10-30 22:30+00'::timestamptz)")
            )
            assert unpinned.astimezone(UTC) == datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
    finally:
        await raw.dispose()

    database = Database(harness_settings(cairo_zoned_database))
    workspace = None
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=1)
        async with database.session_factory() as session:
            assert await session.scalar(text("show timezone")) == "UTC"
            for day in (29, 30):
                session.add(
                    UsageEvent(
                        tenant_id=workspace.tenant_id,
                        event_type=UsageEventType.API_REQUEST,
                        quantity=1,
                        unit=unit_for(UsageEventType.API_REQUEST),
                        occurred_at=datetime(2026, 10, day, 22, 30, tzinfo=UTC),
                    )
                )
            await session.commit()
            points = await UsageEventRepository(session, tenant_id=workspace.tenant_id).daily(
                event_types=[UsageEventType.API_REQUEST]
            )
        assert [(point.day.astimezone(UTC), point.quantity) for point in points] == [
            (datetime(2026, 10, 29, tzinfo=UTC), 1),
            (datetime(2026, 10, 30, tzinfo=UTC), 1),
        ]
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- TZ-08


async def test_a_wall_time_is_resolved_by_the_server_across_both_changes(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="lt-owner@example.com")
    await _workspace(db_session, slug="local-time", owner=owner)
    headers = await _select_workspace(http, await _login(http, owner.email), "local-time")

    winter = await http.get(URL, params={"at": "2026-10-30T09:00"}, headers=headers)
    assert winter.status_code == 200, winter.text
    body = winter.json()
    assert body["at_local"] == "2026-10-30T09:00:00+02:00"
    assert body["at_utc"] == "2026-10-30T07:00:00Z"
    assert (body["timezone"], body["offset"]) == (CAIRO, "+02:00")
    assert (body["ambiguous"], body["nonexistent"]) == (False, False)
    assert body["alternatives"] == ["2026-10-30T09:00:00+02:00"]

    fold = (await http.get(URL, params={"at": "2026-10-29T23:30"}, headers=headers)).json()
    assert fold["ambiguous"] is True and fold["nonexistent"] is False
    assert fold["alternatives"] == ["2026-10-29T23:30:00+03:00", "2026-10-29T23:30:00+02:00"]
    assert fold["at_local"] == "2026-10-29T23:30:00+03:00"
    assert fold["at_utc"] == "2026-10-29T20:30:00Z"

    gap = (await http.get(URL, params={"at": "2027-04-30T00:30"}, headers=headers)).json()
    assert gap["nonexistent"] is True and gap["ambiguous"] is False
    assert gap["at_local"] == "2027-04-30T01:00:00+03:00"
    assert gap["at_utc"] == "2027-04-29T22:00:00Z"

    for refused in ("2026-10-30T09:00+02:00", "2026-10-30T09:00:00", "2026-10-30T09:00Z", "soon"):
        answer = await http.get(URL, params={"at": refused}, headers=headers)
        assert answer.status_code == 422, refused
        assert answer.json()["error"]["code"] == "local_time_invalid"

    assert (await http.get(URL, params={"at": "2026-10-30T09:00"})).status_code == 401


# --------------------------------------------------------------------------- TZ-09


async def test_a_campaign_scheduled_on_the_october_change_day_starts_at_nine_local(
    prepared_database: str,
) -> None:
    """09:00+02:00 on 2026-10-30 is 07:00 UTC; the first send is at 09:00 local, none at night."""
    chosen = datetime.fromisoformat("2026-10-30T09:00:00+02:00")
    assert chosen.astimezone(UTC) == datetime(2026, 10, 30, 7, 0, tzinfo=UTC)
    clock = SimClock(now=cairo(2026, 10, 30, 8, 30))
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name="tz09-right")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=3)
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=chosen)
        async with db.session_factory() as session:
            stored = await session.get(Campaign, campaign_id)
            assert stored is not None and stored.scheduled_at == datetime(
                2026, 10, 30, 7, 0, tzinfo=UTC
            )

        await _drain(clock, replica)
        assert meta.requests == []

        clock.stand(datetime(2026, 10, 30, 7, 0, 5, tzinfo=UTC))
        await _drain(clock, replica)
        assert len(meta.requests) == 3
        assert min(entry["at"] for entry in meta.requests) >= cairo(2026, 10, 30, 9)
        assert sum(1 for entry in meta.requests if WINDOW.contains(entry["at"])) == 0
    finally:
        await erase(db)
        await db.dispose()


@pytest.mark.parametrize("quiet_hours", [True, False])
async def test_the_stale_summer_offset_keeps_its_instant(
    prepared_database: str, quiet_hours: bool
) -> None:
    """09:00+03:00 on 2026-10-30 is 06:00 UTC, which is 08:00 Cairo time that day (ADR-145).

    The instant is kept: it is the client's error, and TZ-08 is its cure. With
    quiet hours on it waits to 09:00; with them off it starts at 08:00 local.
    """
    stale = datetime.fromisoformat("2026-10-30T09:00:00+03:00")
    assert stale.astimezone(UTC) == datetime(2026, 10, 30, 6, 0, tzinfo=UTC)
    clock = SimClock(now=datetime(2026, 10, 30, 6, 0, 5, tzinfo=UTC))
    meta = FakeMeta()
    meta.clock = clock.at
    replica, db = worker(prepared_database, meta, name=f"tz09-stale-{quiet_hours}")
    try:
        await quiesce(db)
        workspace = await seed(db, recipients=3)
        if not quiet_hours:
            async with db.session_factory() as session:
                await session.execute(
                    update(Tenant)
                    .where(Tenant.id == workspace.tenant_id)
                    .values(campaign_quiet_hours_enabled=False)
                )
                await session.commit()
        campaign_id = await compose(db, workspace)
        await make_due(db, campaign_id, at=stale)
        async with db.session_factory() as session:
            stored = await session.get(Campaign, campaign_id)
            assert stored is not None and stored.scheduled_at == datetime(
                2026, 10, 30, 6, 0, tzinfo=UTC
            )

        await _drain(clock, replica)
        if quiet_hours:
            assert meta.requests == []
            async with db.session_factory() as session:
                row = await session.get(Campaign, campaign_id)
                assert row is not None
                assert row.resumes_at == datetime(2026, 10, 30, 7, 0, tzinfo=UTC)
            clock.stand(datetime(2026, 10, 30, 7, 0, 5, tzinfo=UTC))
            await _drain(clock, replica)
            assert len(meta.requests) == 3
            assert min(entry["at"] for entry in meta.requests) >= cairo(2026, 10, 30, 9)
        else:
            assert len(meta.requests) == 3
            first = min(entry["at"] for entry in meta.requests).astimezone(zone(CAIRO))
            assert first.hour == 8 and first - cairo(2026, 10, 30, 8) < timedelta(minutes=1)
    finally:
        await erase(db)
        await db.dispose()
