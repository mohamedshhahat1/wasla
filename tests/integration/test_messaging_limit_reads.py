"""Meta's messaging limit: read, stored per portfolio, kept current, never assumed (LIM-01).

The reads go through the real `MetaMessagingLimitReader` to the fake Meta at
the transport, which answers `GET /{phone-number-id}` for the field
`whatsapp_business_manager_messaging_limit` as "Messaging limits" documents it
(developers.facebook.com, read 2026-10-16). The webhook goes through the real ingestion service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.campaign import Campaign, CampaignStatus
from app.db.models.messaging_limit import WhatsAppBusinessPortfolio
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.session import Database
from app.integrations.whatsapp.messaging_limit import MetaMessagingLimitReader
from app.services.campaign_service import CampaignService
from app.services.messaging_limit_service import MessagingLimitService, limit_for_account
from app.services.whatsapp_account_service import WhatsAppAccountService
from app.services.whatsapp_service import WhatsAppIngestionService
from scripts.read_messaging_limits import read_all
from tests.channel_plans import allow_channels
from tests.fake_ownership import FakeOwnershipVerifier
from tests.integration.campaign_harness import (
    TENANT_PREFIX,
    FakeMeta,
    SimClock,
    compose,
    erase,
    harness_settings,
    meta_error,
    quiesce,
    read_limit,
    seed,
    worker,
)
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit]


def _settings(database: Database) -> Settings:
    return harness_settings(database.engine.url.render_as_string(hide_password=False))


async def portfolio_of(
    database: Database, account_id: uuid.UUID
) -> WhatsAppBusinessPortfolio | None:
    async with database.session_factory() as session:
        found: WhatsAppBusinessPortfolio | None = await session.scalar(
            select(WhatsAppBusinessPortfolio)
            .join(WhatsAppAccount, WhatsAppAccount.portfolio_id == WhatsAppBusinessPortfolio.id)
            .where(WhatsAppAccount.id == account_id)
        )
        return found


async def test_a_read_asks_meta_on_v24_with_the_number_s_credential_and_stores_the_number(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=0)
        await read_limit(database, meta, workspace.account_id)

        assert len(meta.limit_reads) == 1
        read = meta.limit_reads[0]
        assert read["version"] == "v24.0"
        assert read["fields"] == "whatsapp_business_manager_messaging_limit"
        assert read["authorization"] == "Bearer harness-platform-token"
        portfolio = await portfolio_of(database, workspace.account_id)
        assert portfolio is not None
        assert (portfolio.tier, portfolio.messaging_limit, portfolio.recognised) == (
            "TIER_250",
            250,
            True,
        )
        assert portfolio.source == "api" and portfolio.checked_at is not None
    finally:
        await erase(database)
        await database.dispose()


async def test_two_numbers_of_one_portfolio_share_one_row_even_across_workspaces(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    database = Database(harness_settings(prepared_database))
    waba = f"waba-shared-{uuid.uuid4().hex[:8]}"
    try:
        first = await seed(database, recipients=0, waba_id=waba)
        second = await seed(database, recipients=0, waba_id=waba)
        await read_limit(database, meta, first.account_id)
        await read_limit(database, meta, second.account_id)

        one = await portfolio_of(database, first.account_id)
        other = await portfolio_of(database, second.account_id)
        assert one is not None and other is not None
        assert one.id == other.id
        assert one.meta_key == waba and one.key_kind == "waba"
        assert one.messaging_limit == 2_000
    finally:
        await erase(database)
        await database.dispose()


async def test_an_unrecognised_value_is_the_lowest_level_and_recorded_as_such(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="UNTIERED")
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=0)
        await read_limit(database, meta, workspace.account_id)
        portfolio = await portfolio_of(database, workspace.account_id)
        assert portfolio is not None
        assert (portfolio.messaging_limit, portfolio.recognised, portfolio.tier) == (
            250,
            False,
            "UNTIERED",
        )
    finally:
        await erase(database)
        await database.dispose()


async def test_never_read_refuses_the_schedule_and_a_later_read_lets_it_through(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit=meta_error(400, 100))
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=2)
        await read_limit(database, meta, workspace.account_id)
        assert (await portfolio_of(database, workspace.account_id)) is not None
        async with database.session_factory() as session:
            assert not (await limit_for_account(session, workspace.account_id)).known

        campaign_id = await compose(database, workspace, schedule=False)
        async with database.session_factory() as session:
            with pytest.raises(ValidationError) as refused:
                await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
                    campaign_id=campaign_id
                )
        assert refused.value.error_code == "messaging_limit_unknown"
        assert refused.value.status_code == 422

        meta.messaging_limit = "TIER_2K"
        async with database.session_factory() as session:
            service = CampaignService(
                session=session,
                tenant_id=workspace.tenant_id,
                limits=MessagingLimitService(
                    session,
                    settings=_settings(database),
                    reader=MetaMessagingLimitReader(http=meta.client("schedule")),
                ),
            )
            await service.refresh_messaging_limit(campaign_id)
            scheduled = await service.schedule(campaign_id=campaign_id)
            assert scheduled.status is CampaignStatus.SCHEDULED
            await session.commit()
    finally:
        await erase(database)
        await database.dispose()


async def test_a_failed_read_keeps_the_last_value_and_a_stale_one_is_read_again(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=0)
        await read_limit(database, meta, workspace.account_id)
        later = datetime.now(UTC) + timedelta(hours=7)

        meta.messaging_limit = httpx.Response(500)
        async with database.session_factory() as session:
            account = await session.get(WhatsAppAccount, workspace.account_id)
            assert account is not None
            limits = MessagingLimitService(
                session,
                settings=_settings(database),
                reader=MetaMessagingLimitReader(http=meta.client("refresh")),
            )
            kept = await limits.refresh_if_stale(account, at=later)
            assert kept.known and kept.messaging_limit == 2_000
            assert len(meta.limit_reads) == 2

            meta.messaging_limit = "TIER_10K"
            fresh = await limits.refresh_if_stale(account, at=datetime.now(UTC))
            assert fresh.messaging_limit == 2_000, "not stale yet: not read again"
            assert len(meta.limit_reads) == 2
            dropped = await limits.refresh_if_stale(account, at=later)
            assert dropped.messaging_limit == 10_000
            await session.commit()
    finally:
        await erase(database)
        await database.dispose()


def _capability(waba: str, value: object, *, time: int) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba,
                "time": time,
                "changes": [
                    {
                        "field": "business_capability_update",
                        "value": {"max_daily_conversations_per_business": value},
                    }
                ],
            }
        ],
    }


async def test_the_capability_webhook_changes_the_limit_in_order_and_alerts_change_nothing(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    database = Database(harness_settings(prepared_database))
    waba = f"waba-hook-{uuid.uuid4().hex[:8]}"
    try:
        workspace = await seed(database, recipients=0, waba_id=waba)
        await read_limit(database, meta, workspace.account_id)
        now = int(datetime.now(UTC).timestamp())

        async with database.session_factory() as session:
            await WhatsAppIngestionService(session=session).ingest(
                _capability(waba, "TIER_2K", time=now + 60)
            )
            await session.commit()
        portfolio = await portfolio_of(database, workspace.account_id)
        assert portfolio is not None
        assert (portfolio.messaging_limit, portfolio.source) == (2_000, "webhook")

        async with database.session_factory() as session:
            await WhatsAppIngestionService(session=session).ingest(
                _capability(waba, "TIER_250", time=now - 3600)
            )
            await WhatsAppIngestionService(session=session).ingest(
                {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "id": waba,
                            "time": now + 120,
                            "changes": [
                                {
                                    "field": "account_alerts",
                                    "value": {
                                        "alert_type": "INCREASED_CAPABILITIES_ELIGIBILITY_FAILED"
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
            await session.commit()
        portfolio = await portfolio_of(database, workspace.account_id)
        assert portfolio is not None
        assert portfolio.messaging_limit == 2_000, "an older delivery does not overwrite"
    finally:
        await erase(database)
        await database.dispose()


async def test_a_running_campaign_on_a_number_nobody_has_read_waits_and_then_sends(
    prepared_database: str,
) -> None:
    """Fail closed (LIM-01): after 0110 every number starts unknown until the deploy step."""
    meta = FakeMeta(messaging_limit="TIER_2K")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="limit-unknown")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=3)
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await compose(database, workspace)
        async with database.session_factory() as session:
            await session.execute(
                update(WhatsAppAccount)
                .where(WhatsAppAccount.id == workspace.account_id)
                .values(portfolio_id=None)
            )
            await session.commit()

        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await clock.sweep(replica)
        async with database.session_factory() as session:
            row = await session.get(Campaign, campaign_id)
            assert row is not None
            assert row.waiting_reason == "limit_unknown"
            assert row.resumes_at is not None
            waited = row.resumes_at - clock.now
            assert timedelta(minutes=5) <= waited < timedelta(minutes=5, seconds=10)
        assert meta.requests == []

        async with database.session_factory() as session:
            outcomes = await read_all(
                database,
                _settings(database),
                reader=MetaMessagingLimitReader(http=meta.client("deploy-step")),
            )
            assert any(outcome.account_id == str(workspace.account_id) for outcome in outcomes)
        clock.now = clock.now + timedelta(minutes=6)
        for _ in range(5):
            if await clock.sweep(replica) == 0:
                break
        assert len(meta.requests) == 3
    finally:
        await erase(database)
        await database.dispose()


async def test_connect_and_enable_read_the_limit_before_any_lock(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    database = Database(harness_settings(prepared_database))
    number = f"10{uuid.uuid4().int % 10**10:010d}"
    try:
        async with database.session_factory() as session:
            tenant = Tenant(
                timezone=TEST_TIMEZONE,
                name="Connect",
                slug=f"{TENANT_PREFIX}{uuid.uuid4().hex[:10]}",
            )
            session.add(tenant)
            await session.flush()
            await allow_channels(session, tenant.id)
            service = WhatsAppAccountService(
                session=session,
                ownership=FakeOwnershipVerifier().owns(number, waba_id=f"waba-{number}"),
                limits=MessagingLimitService(
                    session,
                    settings=_settings(database),
                    reader=MetaMessagingLimitReader(http=meta.client("connect")),
                ),
            )
            account = await service.connect(
                tenant_id=tenant.id, phone_number_id=number, access_token="workspace-token"
            )
            await session.commit()
            account_id = account.id
        assert meta.limit_reads[-1]["authorization"] == "Bearer workspace-token"
        portfolio = await portfolio_of(database, account_id)
        assert portfolio is not None and portfolio.messaging_limit == 250

        meta.messaging_limit = "TIER_10K"
        async with database.session_factory() as session:
            service = WhatsAppAccountService(
                session=session,
                limits=MessagingLimitService(
                    session,
                    settings=_settings(database),
                    reader=MetaMessagingLimitReader(http=meta.client("enable")),
                ),
            )
            await service.set_status(
                tenant_id=tenant.id, account_id=account_id, status=WhatsAppAccountStatus.DISABLED
            )
            await service.set_status(
                tenant_id=tenant.id, account_id=account_id, status=WhatsAppAccountStatus.ACTIVE
            )
            await session.commit()
        portfolio = await portfolio_of(database, account_id)
        assert portfolio is not None and portfolio.messaging_limit == 10_000
    finally:
        await erase(database)
        await database.dispose()


async def test_the_deploy_step_is_idempotent_and_lists_what_it_could_not_read(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=0)
        reader = MetaMessagingLimitReader(http=meta.client("deploy"))
        first = await read_all(database, _settings(database), reader=reader)
        second = await read_all(database, _settings(database), reader=reader)
        mine = [o for o in first + second if o.account_id == str(workspace.account_id)]
        assert [(o.read, o.known, o.messaging_limit) for o in mine] == [
            (True, True, 2_000),
            (True, True, 2_000),
        ]
        stale_only = await read_all(database, _settings(database), reader=reader, stale_only=True)
        assert str(workspace.account_id) not in {o.account_id for o in stale_only}

        meta.messaging_limit = meta_error(403, 200)
        failed = await read_all(database, _settings(database), reader=reader)
        mine = [o for o in failed if o.account_id == str(workspace.account_id)]
        assert [(o.read, o.known, o.messaging_limit) for o in mine] == [(False, True, 2_000)]
    finally:
        await erase(database)
        await database.dispose()
