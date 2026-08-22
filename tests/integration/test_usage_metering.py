"""Metering against a real database.

What only PostgreSQL can prove is here: that the aggregates group and sum the
way a bill needs them to, that the window boundary is half-open so two adjacent
months add up to the pair, and that one workspace's consumption is invisible to
another - including to the aggregate queries, which are the reads most likely to
forget a tenant filter because the filter is not what they are about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.repositories.usage_repository import PlatformUsageRepository, UsageEventRepository
from app.services.usage_service import UsageRecorder, UsageService

pytestmark = pytest.mark.integration

SINCE = datetime(2026, 8, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)
INSIDE = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


async def _tenant(session, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def test_a_recorded_meter_lands_with_its_own_unit(db_session):
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)

    recorder.record(UsageEventType.AI_INPUT_TOKEN, quantity=1200, occurred_at=INSIDE)
    await db_session.flush()

    stored = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(stored) == 1
    assert stored[0].unit is UsageUnit.TOKEN
    assert stored[0].quantity == 1200
    assert stored[0].tenant_id == tenant.id
    assert stored[0].occurred_at == INSIDE


async def test_nothing_is_stored_for_nothing_consumed(db_session):
    """A model that reported no output tokens is not a row; it is an absence.

    Storing it would mean scanning rows that can never change a sum.
    """
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)

    assert recorder.record(UsageEventType.AI_OUTPUT_TOKEN, quantity=0) is None
    assert recorder.record(UsageEventType.STORAGE_USED, quantity=-5) is None
    await db_session.flush()

    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 0


async def test_one_agent_turn_meters_the_request_and_both_token_counts(db_session):
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)

    recorder.ai_request(
        input_tokens=800,
        output_tokens=120,
        model="gpt-5.1",
        occurred_at=INSIDE,
    )
    await db_session.flush()

    totals = await UsageEventRepository(db_session, tenant_id=tenant.id).totals()
    by_type = {total.event_type: total.quantity for total in totals}
    assert by_type[UsageEventType.AI_REQUEST] == 1
    assert by_type[UsageEventType.AI_INPUT_TOKEN] == 800
    assert by_type[UsageEventType.AI_OUTPUT_TOKEN] == 120

    stored = (await db_session.execute(select(UsageEvent))).scalars().all()
    # The model is on every line, because a token from one model is not a token
    # from another and a bill that cannot say which was used cannot be checked.
    assert {row.meta["model"] for row in stored} == {"gpt-5.1"}


async def test_a_turn_that_rolls_back_is_not_billed(db_session):
    """The whole reason metering shares the caller's transaction."""
    tenant = await _tenant(db_session, "acme")
    await db_session.commit()

    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.AI_REQUEST, occurred_at=INSIDE)
    await db_session.flush()
    await db_session.rollback()

    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 0


async def test_the_window_is_half_open(db_session):
    """Two adjacent windows must sum to the pair, which a closed upper bound
    would break by counting a midnight row in both."""
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)

    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, occurred_at=SINCE)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, occurred_at=INSIDE)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, occurred_at=UNTIL)
    await db_session.flush()

    totals = await UsageEventRepository(db_session, tenant_id=tenant.id).totals(
        since=SINCE,
        until=UNTIL,
    )
    assert [total.quantity for total in totals] == [2]


async def test_a_meter_can_be_asked_for_on_its_own(db_session):
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.RAG_QUERY, occurred_at=INSIDE)
    recorder.record(UsageEventType.LEAD_CREATED, occurred_at=INSIDE)
    await db_session.flush()

    totals = await UsageEventRepository(db_session, tenant_id=tenant.id).totals(
        event_types=[UsageEventType.RAG_QUERY],
    )
    assert [(total.event_type, total.quantity) for total in totals] == [
        (UsageEventType.RAG_QUERY, 1)
    ]


async def test_a_series_has_one_point_per_day_per_meter(db_session):
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_RECEIVED, occurred_at=INSIDE)
    recorder.record(
        UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
        occurred_at=INSIDE + timedelta(hours=3),
    )
    recorder.record(
        UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
        occurred_at=INSIDE + timedelta(days=1),
    )
    await db_session.flush()

    points = await UsageEventRepository(db_session, tenant_id=tenant.id).daily(
        since=SINCE,
        until=UNTIL,
    )
    assert [(point.day.date().isoformat(), point.quantity) for point in points] == [
        ("2026-08-15", 2),
        ("2026-08-16", 1),
    ]


async def test_a_summary_names_the_counters_a_dashboard_asks_for(db_session):
    tenant = await _tenant(db_session, "acme")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    now = datetime.now(UTC)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_RECEIVED, occurred_at=now)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, occurred_at=now)
    recorder.ai_request(input_tokens=300, output_tokens=45, occurred_at=now)
    await db_session.flush()

    summary = await UsageService(db_session, tenant_id=tenant.id).summary()
    assert summary.messages_received == 1
    assert summary.messages_sent == 1
    assert summary.ai_requests == 1
    assert summary.total_tokens == 345


async def test_one_workspace_cannot_see_anothers_consumption(db_session):
    """The aggregate reads are where a tenant filter is easiest to forget: the
    query is about summing, not about ownership."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")

    UsageRecorder(db_session, tenant_id=acme.id).record(
        UsageEventType.AI_REQUEST,
        occurred_at=INSIDE,
    )
    UsageRecorder(db_session, tenant_id=rival.id).record(
        UsageEventType.AI_REQUEST,
        quantity=999,
        occurred_at=INSIDE,
    )
    await db_session.flush()

    totals = await UsageEventRepository(db_session, tenant_id=acme.id).totals()
    assert [total.quantity for total in totals] == [1]

    points = await UsageEventRepository(db_session, tenant_id=acme.id).daily()
    assert [point.quantity for point in points] == [1]


async def test_the_platform_reader_sums_across_workspaces(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    UsageRecorder(db_session, tenant_id=acme.id).record(
        UsageEventType.AI_REQUEST,
        occurred_at=INSIDE,
    )
    UsageRecorder(db_session, tenant_id=rival.id).record(
        UsageEventType.AI_REQUEST,
        quantity=4,
        occurred_at=INSIDE,
    )
    await db_session.flush()

    platform = PlatformUsageRepository(db_session)
    totals = await platform.totals(since=SINCE, until=UNTIL)
    assert [(total.event_type, total.quantity) for total in totals] == [
        (UsageEventType.AI_REQUEST, 5)
    ]

    per_tenant = await platform.by_tenant(since=SINCE, until=UNTIL)
    assert {row.tenant_id: row.quantity for row in per_tenant} == {acme.id: 1, rival.id: 4}


async def test_the_platform_reader_can_be_narrowed_to_a_page_of_workspaces(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    for tenant in (acme, rival):
        UsageRecorder(db_session, tenant_id=tenant.id).record(
            UsageEventType.AI_REQUEST,
            occurred_at=INSIDE,
        )
    await db_session.flush()

    rows = await PlatformUsageRepository(db_session).by_tenant(tenant_ids=[acme.id])
    assert [row.tenant_id for row in rows] == [acme.id]
