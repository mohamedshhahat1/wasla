"""A campaign due and not swept shows up as stuck at scrape time (CMP-23).

`wasla_campaigns_stuck` is what `CampaignsStuck` alerts on. A campaign whose
moment came more than fifteen minutes ago and that no sweep has moved counts;
one a sweep has just taken - its `next_send_at` pushed to a lease - does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.db.models.campaign import Campaign
from app.services.metrics_service import MetricsService
from tests.integration.campaign_harness import FakeMeta, compose, erase, quiesce, seed, worker

pytestmark = pytest.mark.integration


async def test_an_overdue_campaign_is_counted_stuck_until_a_sweep_takes_it(
    prepared_database: str,
) -> None:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="metrics")
    await quiesce(database)
    try:
        workspace = await seed(database, recipients=2)
        campaign_id = await compose(database, workspace)
        async with database.session_factory() as session:
            await session.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(scheduled_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await session.commit()

        lines = await MetricsService(None, database=database)._campaign_lines(now=None)
        assert "wasla_campaigns_stuck 1" in lines

        await each.run_once()

        lines = await MetricsService(None, database=database)._campaign_lines(now=None)
        assert "wasla_campaigns_stuck 0" in lines
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()
