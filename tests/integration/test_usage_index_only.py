"""The entitlement check reads its numbers from an index. Do they still add up?

`ix_usage_events_tenant_id_event_type_occurred_at` carries `quantity` and
`unit` so the sum never visits the table (ADR-081). That is a change to *how*
PostgreSQL answers the hottest read in the system, and usage is what bills a
customer - so the question this file asks is not whether it is faster but
whether it is the same.

The comparison is against the table itself, with every index refused, which is
the only source of truth there is. If the two ever disagree the index is wrong,
and a workspace is being billed from it.

`test_usage_metering.py` covers the meanings - units, half-open windows, tenant
isolation. This covers the mechanism underneath them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.repositories.usage_repository import UsageEventRepository
from app.services.usage_service import UsageRecorder

pytestmark = pytest.mark.integration

INDEX = "ix_usage_events_tenant_id_event_type_occurred_at"
SINCE = datetime(2026, 8, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)
DAYS = 8
PER_DAY = 6


async def _tenant(session, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _seed(session, *, tenant: Tenant, offset: int) -> int:
    """Several days of two meters, and return what `ai_request` should total.

    `offset` makes each workspace's quantities different, so a query that lost
    its tenant filter would produce a number that is wrong rather than one that
    happens to match.
    """
    recorder = UsageRecorder(session, tenant_id=tenant.id)
    expected = 0
    for day in range(DAYS):
        moment = SINCE + timedelta(days=day, hours=9)
        for index in range(PER_DAY):
            quantity = 1 + offset + index
            recorder.record(
                UsageEventType.AI_REQUEST,
                quantity=quantity,
                occurred_at=moment + timedelta(minutes=index),
            )
            expected += quantity
            # A second meter in the same window, so narrowing to one is a real
            # narrowing rather than a no-op.
            recorder.record(
                UsageEventType.AI_INPUT_TOKEN,
                quantity=900 + offset,
                occurred_at=moment + timedelta(minutes=index),
            )
    await session.flush()
    return expected


async def _raw_total(session, *, tenant: Tenant) -> int:
    """The source of truth: the table, with every index refused."""
    async with session.begin_nested():
        await session.execute(text("SET LOCAL enable_indexscan = off"))
        await session.execute(text("SET LOCAL enable_indexonlyscan = off"))
        await session.execute(text("SET LOCAL enable_bitmapscan = off"))
        total = await session.scalar(
            text(
                "SELECT coalesce(sum(quantity), 0) FROM usage_events"
                " WHERE tenant_id = :tenant AND event_type = 'ai_request'"
                "   AND occurred_at >= :since AND occurred_at < :until"
            ),
            {"tenant": tenant.id, "since": SINCE, "until": UNTIL},
        )
    return int(total or 0)


async def _optimised_total(session, *, tenant: Tenant) -> int:
    """What `EntitlementService._period_usage` asks for, through the repository."""
    totals = await UsageEventRepository(session, tenant_id=tenant.id).totals(
        since=SINCE,
        until=UNTIL,
        event_types=[UsageEventType.AI_REQUEST],
    )
    return sum(total.quantity for total in totals)


async def test_the_index_carries_the_columns_the_sum_needs(db_connection):
    """A schema property: `quantity` and `unit` are INCLUDE columns.

    Read off `pg_index`, where the included columns are the attributes past
    `indnkeyatts`. Asserting on the definition text would fail on a
    reformatting and pass on a column added to the wrong half.
    """
    row = (
        await db_connection.execute(
            text(
                "SELECT array_agg(a.attname ORDER BY k.ord) AS included"
                " FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)"
                " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum"
                " WHERE c.relname = :name AND k.ord > i.indnkeyatts"
                " GROUP BY i.indexrelid"
            ),
            {"name": INDEX},
        )
    ).one()

    assert set(row.included) == {"quantity", "unit"}


async def test_the_period_sum_is_answered_from_the_index_alone(db_session):
    """The plan, not the timing.

    The narrower indexes are dropped and sequential scans refused, inside a
    savepoint the test rolls back. On a few hundred rows PostgreSQL prefers
    whichever index is smallest and filters the rest, and it is right to - what
    is under test is not which plan it picks at this size but whether this
    index *can* answer without the table. `Index Only Scan` is PostgreSQL
    saying it can: it will not choose that node unless every column the query
    reads is available from the index, so removing the INCLUDE columns leaves a
    plain `Index Scan` and fails this.

    `Heap Fetches` is deliberately not asserted. These rows are written inside
    a transaction that never commits, so no page is ever marked all-visible and
    an index-only scan here always falls back to the heap for visibility. On a
    committed, vacuumed table it reads zero, which is a local measurement
    recorded in ADR-081 rather than something this fixture can show.
    """
    tenant = await _tenant(db_session, "plan-check")
    await _seed(db_session, tenant=tenant, offset=0)

    async with db_session.begin_nested():
        await db_session.execute(text("DROP INDEX ix_usage_events_tenant_id_occurred_at"))
        await db_session.execute(text("DROP INDEX ix_usage_events_tenant_id"))
        await db_session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in await db_session.execute(
                text(
                    "EXPLAIN SELECT event_type, unit, sum(quantity), count(*)"
                    " FROM usage_events"
                    " WHERE tenant_id = :tenant AND event_type = 'ai_request'"
                    "   AND occurred_at >= :since AND occurred_at < :until"
                    " GROUP BY event_type, unit"
                ),
                {"tenant": tenant.id, "since": SINCE, "until": UNTIL},
            )
        )

    assert f"Index Only Scan using {INDEX}" in plan


async def test_the_optimised_total_equals_the_raw_total_for_every_workspace(db_session):
    """GATE: the index and the table agree, across workspaces and days."""
    tenants = [await _tenant(db_session, f"agree-{index}") for index in range(3)]
    expected = {
        tenant.id: await _seed(db_session, tenant=tenant, offset=index * 7)
        for index, tenant in enumerate(tenants)
    }

    for tenant in tenants:
        raw = await _raw_total(db_session, tenant=tenant)
        optimised = await _optimised_total(db_session, tenant=tenant)

        assert optimised == raw
        assert optimised == expected[tenant.id]

    # Different by construction, so "they agree" is not "they are all the same
    # number and any filter would have passed".
    assert len(set(expected.values())) == len(tenants)


async def test_the_two_agree_on_the_boundary_rows(db_session):
    """Half-open, and answered the same way whichever plan runs.

    One row on each boundary and one in between. `since` counts, `until` does
    not, and the index must not change that - an off-by-one in a range scan is
    exactly the shape of bug an index-only path could introduce and a totals
    assertion on interior rows would never see.
    """
    tenant = await _tenant(db_session, "boundary")
    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.AI_REQUEST, quantity=100, occurred_at=SINCE)
    recorder.record(
        UsageEventType.AI_REQUEST,
        quantity=20,
        occurred_at=SINCE + timedelta(days=3),
    )
    recorder.record(UsageEventType.AI_REQUEST, quantity=3, occurred_at=UNTIL)
    # A microsecond inside the upper bound, which must count.
    recorder.record(
        UsageEventType.AI_REQUEST,
        quantity=7,
        occurred_at=UNTIL - timedelta(microseconds=1),
    )
    await db_session.flush()

    raw = await _raw_total(db_session, tenant=tenant)
    optimised = await _optimised_total(db_session, tenant=tenant)

    assert optimised == raw == 127


async def test_an_event_recorded_late_lands_in_the_window_it_happened_in(db_session):
    """A worker draining a backlog records when the thing happened.

    `usage_events` has no insertion timestamp - `occurred_at` is the only time
    on the row, deliberately, so replaying a backlog cannot smear yesterday's
    consumption into today's bill. This writes a row for a day that is already
    over and asserts both paths move by the same amount.
    """
    tenant = await _tenant(db_session, "late")
    before = await _seed(db_session, tenant=tenant, offset=0)
    assert await _optimised_total(db_session, tenant=tenant) == before

    UsageRecorder(db_session, tenant_id=tenant.id).record(
        UsageEventType.AI_REQUEST,
        quantity=41,
        occurred_at=SINCE + timedelta(days=1, hours=4),
    )
    # And one that belongs to a window already closed, which must not appear.
    UsageRecorder(db_session, tenant_id=tenant.id).record(
        UsageEventType.AI_REQUEST,
        quantity=1_000,
        occurred_at=SINCE - timedelta(days=1),
    )
    await db_session.flush()

    raw = await _raw_total(db_session, tenant=tenant)
    optimised = await _optimised_total(db_session, tenant=tenant)

    assert optimised == raw == before + 41


async def test_the_index_does_not_leak_another_workspaces_quantities(db_session):
    """The filter is still a filter.

    An index shared by every workspace is a scan that touches every workspace's
    entries, so the tenant predicate is doing the same work it always did -
    and is worth asserting on the new plan rather than assuming it carried
    over.
    """
    mine = await _tenant(db_session, "mine")
    theirs = await _tenant(db_session, "theirs")
    expected = await _seed(db_session, tenant=mine, offset=0)
    await _seed(db_session, tenant=theirs, offset=100)

    assert await _optimised_total(db_session, tenant=mine) == expected
    assert await _raw_total(db_session, tenant=mine) == expected
    assert await _optimised_total(db_session, tenant=theirs) != expected


@pytest.mark.parametrize(
    "meters",
    [
        [UsageEventType.AI_REQUEST],
        [UsageEventType.AI_INPUT_TOKEN],
        [UsageEventType.AI_REQUEST, UsageEventType.AI_INPUT_TOKEN],
    ],
)
async def test_narrowing_to_meters_agrees_with_the_table(db_session, meters):
    """Every shape `PERIOD_METERS` can ask for, checked against a scan."""
    tenant = await _tenant(db_session, "meters")
    await _seed(db_session, tenant=tenant, offset=3)

    totals = await UsageEventRepository(db_session, tenant_id=tenant.id).totals(
        since=SINCE,
        until=UNTIL,
        event_types=meters,
    )
    optimised = sum(total.quantity for total in totals)

    async with db_session.begin_nested():
        await db_session.execute(text("SET LOCAL enable_indexscan = off"))
        await db_session.execute(text("SET LOCAL enable_indexonlyscan = off"))
        await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
        raw = await db_session.scalar(
            text(
                "SELECT coalesce(sum(quantity), 0) FROM usage_events"
                " WHERE tenant_id = :tenant AND occurred_at >= :since"
                "   AND occurred_at < :until"
                "   AND event_type = ANY(CAST(:meters AS usage_event_type[]))"
            ),
            {
                "tenant": tenant.id,
                "since": SINCE,
                "until": UNTIL,
                "meters": [meter.value for meter in meters],
            },
        )

    assert optimised == int(raw or 0)
    assert optimised > 0
