"""Recipients listed by cursor with reason codes; composition audited; time needs an offset.

CMP-13, CMP-14, CMP-15, CMP-22. On the audited head the recipients listing
stopped at 100 with no cursor and prose reasons, creating a campaign and
setting its audience left no audit entry, a `scheduled_at` without an offset was
taken as UTC (a Cairo admin's 10:00 became 13:00 local), and nothing in the
database bounded `messages_per_minute`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ValidationError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    OptOutSource,
    OptOutVia,
    RecipientReason,
    RecipientStatus,
)
from app.db.models.channel import Channel
from app.db.models.user import User
from app.db.session import Database
from app.repositories.campaign_repository import AudienceFilter, CampaignRecipientRepository
from app.services.campaign_service import NAIVE_SCHEDULE_CODE, CampaignService
from app.services.opt_out import record_resume
from tests.consent import consent_of
from tests.integration.campaign_harness import (
    USER_DOMAIN,
    FakeMeta,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="surface")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


async def test_every_recipient_can_be_listed_by_cursor(database: Database) -> None:
    """A 250-person campaign, 100 a page: everyone, once, in order (audit: first 100 only)."""
    workspace = await seed(database, recipients=250)
    campaign_id = await compose(database, workspace, schedule=False)

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        for _ in range(5):
            page = await service.list_recipients(campaign_id, limit=100, cursor=cursor)
            seen.extend(row.id for row in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

    assert len(seen) == 250
    assert len(set(seen)) == 250
    assert seen == sorted(seen)


async def test_recipients_are_filtered_by_reason_code(database: Database) -> None:
    workspace = await seed(database, recipients=4)
    campaign_id = await compose(database, workspace, schedule=False)
    async with database.session_factory() as session:
        first = (
            (
                await session.execute(
                    select(CampaignRecipient)
                    .where(CampaignRecipient.campaign_id == campaign_id)
                    .order_by(CampaignRecipient.id)
                    .limit(1)
                )
            )
            .scalars()
            .one()
        )
        first.status = RecipientStatus.SKIPPED
        first.reason_code = RecipientReason.OPTED_OUT.value
        await session.commit()

    async with database.session_factory() as session:
        page = await CampaignService(
            session=session, tenant_id=workspace.tenant_id
        ).list_recipients(campaign_id, reason=RecipientReason.OPTED_OUT)
    assert [row.id for row in page.items] == [first.id]
    assert page.items[0].reason_code == RecipientReason.OPTED_OUT


async def test_creating_a_campaign_and_setting_its_audience_are_audited(
    database: Database,
) -> None:
    """CMP-14: who composed it and for how many, never the people or the parameters."""
    workspace = await seed(database, recipients=3)
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Audited",
        )
        await service.set_audience(
            campaign_id=campaign.id,
            filters=AudienceFilter(contact_ids=tuple(workspace.contact_ids)),
        )
        await session.commit()
        campaign_id = campaign.id

    async with database.session_factory() as session:
        entries = {
            entry.action: entry
            for entry in (
                await session.execute(select(AuditLog).where(AuditLog.target_id == campaign_id))
            ).scalars()
        }
    assert AuditAction.CAMPAIGN_CREATED in entries
    audience = entries[AuditAction.CAMPAIGN_AUDIENCE_SET]
    assert audience.meta is not None
    assert audience.meta["recipients"] == 3
    assert audience.meta["filters"] == {
        "last_inbound_within_days": None,
        "lead_statuses": [],
        "contact_ids": 3,
    }
    text = str(audience.meta) + str(entries[AuditAction.CAMPAIGN_CREATED].meta)
    assert all(str(contact) not in text for contact in workspace.contact_ids)


async def test_a_scheduled_time_without_an_offset_is_refused(database: Database) -> None:
    """CMP-15: refused, not silently taken as UTC; an aware time is unchanged."""
    workspace = await seed(database, recipients=1)
    campaign_id = await compose(database, workspace, schedule=False)
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        with pytest.raises(ValidationError) as raised:
            await service.schedule(
                campaign_id=campaign_id, scheduled_at=datetime(2026, 10, 14, 10, 0)
            )
        assert raised.value.error_code == NAIVE_SCHEDULE_CODE

    # Built from the real clock (CPM-02): `schedule` keeps a future time as it
    # was given but moves a past one to now, so a fixed date stopped proving
    # anything the day it passed. Two days from now, 10:00 at +03:00.
    plus_three = timezone(timedelta(hours=3))
    cairo = (datetime.now(plus_three) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    assert cairo - datetime.now(UTC) > timedelta(days=1), "a future time: a past one moves to now"
    async with database.session_factory() as session:
        campaign = await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id, scheduled_at=cairo
        )
        assert campaign.scheduled_at == cairo
        assert campaign.scheduled_at.utcoffset() is not None
        await session.commit()


@pytest.mark.parametrize("rate", [0, 601])
async def test_the_database_bounds_the_send_rate(database: Database, rate: int) -> None:
    """CMP-22: not only the API schema and the service - the column itself."""
    workspace = await seed(database, recipients=1)
    campaign_id = await compose(database, workspace, schedule=False)
    async with database.session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Campaign).where(Campaign.id == campaign_id).values(messages_per_minute=rate)
            )
            await session.flush()


async def test_the_service_refuses_a_rate_above_the_ceiling(database: Database) -> None:
    """M18: the service's own bound, not only the request schema's and the column's."""
    workspace = await seed(database, recipients=1)
    async with database.session_factory() as session:
        with pytest.raises(ValidationError):
            await CampaignService(session=session, tenant_id=workspace.tenant_id).create(
                account_id=workspace.account_id,
                template_id=workspace.template_id,
                name="Too fast",
                messages_per_minute=601,
            )


async def test_the_recipient_listing_is_tenant_scoped_on_its_own(database: Database) -> None:
    """M24: another workspace's campaign id lists nothing, even past the service's lookup."""
    mine = await seed(database, recipients=3)
    theirs = await seed(database, recipients=1)
    campaign_id = await compose(database, mine, schedule=False)
    async with database.session_factory() as session:
        rows = await CampaignRecipientRepository(
            session, tenant_id=theirs.tenant_id
        ).list_for_campaign(campaign_id)
    assert rows == []


async def test_an_admin_pause_is_audited_with_its_actor(database: Database) -> None:
    """CMP-14: stopping a broadcast part-way names who did it."""
    workspace = await seed(database, recipients=2)
    campaign_id = await compose(database, workspace)
    async with database.session_factory() as session:
        admin = User(email=f"pauser-{uuid.uuid4().hex[:8]}{USER_DOMAIN}", hashed_password="x")
        session.add(admin)
        await session.flush()
        await CampaignService(session=session, tenant_id=workspace.tenant_id).pause(
            campaign_id, actor=admin
        )
        await session.commit()
        admin_id = admin.id
    async with database.session_factory() as session:
        entries = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.target_id == campaign_id,
                        AuditLog.action == AuditAction.CAMPAIGN_PAUSED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(entry.actor_id, entry.meta) for entry in entries] == [
        (admin_id, {"reason": "admin_paused"})
    ]


async def test_a_newer_resume_is_never_overwritten_by_an_older_one(database: Database) -> None:
    """M13: newer resume evidence wins at the writer, whatever order it arrives in (OMNI-030)."""
    workspace = await seed(database, recipients=1)
    newer = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    async with database.session_factory() as session:
        for at in (newer, newer - timedelta(days=3)):
            await record_resume(
                session,
                tenant_id=workspace.tenant_id,
                contact_id=workspace.contact_ids[0],
                channel=Channel.WHATSAPP,
                source=OptOutSource.CUSTOMER,
                via=OptOutVia.PROVIDER_PREFERENCE,
                at=at,
            )
        await session.commit()
    async with database.session_factory() as session:
        consent = await consent_of(session, workspace.contact_ids[0], Channel.WHATSAPP)
    assert consent is not None
    assert consent.resumed_at == newer
