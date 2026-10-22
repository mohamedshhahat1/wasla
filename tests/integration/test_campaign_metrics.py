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


@pytest.mark.messaging_limit
async def test_portfolios_are_counted_by_how_full_their_day_is(prepared_database: str) -> None:
    """`wasla_portfolio_limit_utilisation{bucket}` counts portfolios, never names one (LIM-07)."""
    from sqlalchemy import select

    from app.db.models.business_initiated import BusinessInitiatedSend
    from app.db.models.whatsapp import WhatsAppAccount
    from tests.integration.campaign_harness import read_limit

    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="utilisation")
    await quiesce(database)
    try:
        workspace = await seed(database, recipients=1)
        await read_limit(database, meta, workspace.account_id)
        before = await MetricsService(None, database=database)._campaign_lines(now=None)
        async with database.session_factory() as session:
            portfolio_id = await session.scalar(
                select(WhatsAppAccount.portfolio_id).where(
                    WhatsAppAccount.id == workspace.account_id
                )
            )
            session.add_all(
                BusinessInitiatedSend(
                    portfolio_id=portfolio_id,
                    recipient=f"person-{index}",
                    sent_at=datetime.now(UTC),
                    source="follow_up",
                    tenant_id=workspace.tenant_id,
                )
                for index in range(250)
            )
            await session.commit()
        after = await MetricsService(None, database=database)._campaign_lines(now=None)

        def bucket(lines: list[str], name: str) -> float:
            prefix = f'wasla_portfolio_limit_utilisation{{bucket="{name}"}} '
            return float(next(line for line in lines if line.startswith(prefix))[len(prefix) :])

        assert bucket(after, ">=100") == bucket(before, ">=100") + 1
        assert bucket(after, "<50") == bucket(before, "<50") - 1
        assert not any("portfolio_id" in line for line in after)
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()
