"""A campaign never takes a portfolio past its share of Meta's messaging limit (LIM-03, LIM-04).

Real `CampaignWorker`s with their own databases and commits, the real
`MessagingService`, `WhatsAppClient` and classifier; Meta is replaced at the
HTTP transport, which stamps every request with the worker's clock.
**"Never exceeded" is counted at the transport**: unique recipients of one
portfolio in every moving 24 hours, never Wasla's own rows. Every recipient's
service window is closed (they last wrote two days before) unless a test says
otherwise, so every send counts toward Meta's limit.

Meta's limit: "the maximum number of unique WhatsApp user phone numbers your
business can deliver messages to, outside of a customer service window, within
a moving 24-hour period", per business portfolio ("Messaging limits",
developers.facebook.com, read 2026-10-16). TIER_250 is 250; campaigns get
floor(0.8 x 250) = 200.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageOrigin,
)
from app.db.models.enums import TenantRole
from app.db.models.messaging_limit import WhatsAppBusinessPortfolio
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.repositories.campaign_repository import AudienceFilter
from app.services.campaign_service import CampaignService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    BODY_ONLY,
    FakeMeta,
    SimClock,
    Workspace,
    accepted,
    erase,
    harness_settings,
    meta_error,
    quiesce,
    read_limit,
    seed,
    worker,
)

pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit]

DAY = timedelta(hours=24)


# --------------------------------------------------------------------------- helpers


async def add_contacts(
    database: Database,
    workspace: Workspace,
    count: int,
    *,
    last_inbound_at: datetime,
    account_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """More people who wrote to this number (or `account_id`), at `last_inbound_at`."""
    account = account_id or workspace.account_id
    found: list[uuid.UUID] = []
    async with database.session_factory() as session:
        base = uuid.uuid4().int % 10**6
        for index in range(count):
            contact = Contact(tenant_id=workspace.tenant_id, wa_id=f"21{base:06d}{index:05d}")
            session.add(contact)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=workspace.tenant_id,
                    contact_id=contact.id,
                    account_id=account,
                    status=ConversationStatus.OPEN,
                    mode=ConversationMode.AI,
                    last_inbound_at=last_inbound_at,
                )
            )
            found.append(contact.id)
        await session.commit()
    return found


async def add_number(
    database: Database, workspace: Workspace, *, recipients: int, waba_id: str
) -> Workspace:
    """A second number of the same workspace, on `waba_id`, with its own template and people."""
    suffix = uuid.uuid4().hex[:10]
    async with database.session_factory() as session:
        account = WhatsAppAccount(
            tenant_id=workspace.tenant_id,
            phone_number_id=f"pn2-{suffix}",
            waba_id=waba_id,
            display_phone_number="+201000000001",
        )
        session.add(account)
        await session.flush()
        template = WhatsAppTemplate(
            tenant_id=workspace.tenant_id,
            account_id=account.id,
            name=f"offer_{suffix}",
            language="ar_EG",
            category=TemplateCategory.MARKETING,
            status=TemplateStatus.APPROVED,
            body_text="Hello.",
            variable_count=0,
            components=list(BODY_ONLY),
        )
        session.add(template)
        await session.commit()
        account_id, template_id = account.id, template.id
    contacts = await add_contacts(
        database,
        workspace,
        recipients,
        last_inbound_at=datetime.now(UTC) - timedelta(days=2),
        account_id=account_id,
    )
    return Workspace(workspace.tenant_id, account_id, template_id, contacts)


async def launch(
    database: Database,
    workspace: Workspace,
    *,
    contact_ids: Sequence[uuid.UUID] | None = None,
    rate: int = 600,
) -> uuid.UUID:
    """Compose, target (optionally a slice of the people) and schedule now."""
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Limit",
            messages_per_minute=rate,
        )
        await service.set_audience(
            campaign_id=campaign.id,
            filters=AudienceFilter(contact_ids=tuple(contact_ids or ())),
        )
        await session.commit()
        campaign_id = campaign.id
    async with database.session_factory() as session:
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id
        )
        await session.commit()
    return campaign_id


async def drive(
    clock: SimClock,
    workers_: Iterable[CampaignWorker],
    database: Database,
    campaign_ids: Sequence[uuid.UUID],
    *,
    horizon: timedelta = timedelta(days=5),
    rounds: int = 5000,
) -> None:
    """Sweep with every worker at once, then move the clock to the next moment anything is due."""
    replicas = list(workers_)
    end = clock.now + horizon
    for _ in range(rounds):
        # Every replica's sweep ends before a failure is raised: a sweep that
        # failed must not leave the others sending while the test's teardown
        # erases, where they deadlock with it and hide the failure (FRM-02).
        outcomes = await asyncio.gather(
            *(clock.sweep(replica) for replica in replicas), return_exceptions=True
        )
        failure = next((each for each in outcomes if isinstance(each, BaseException)), None)
        if failure is not None:
            raise failure
        async with database.session_factory() as session:
            rows = (
                await session.execute(
                    select(Campaign.status, Campaign.next_send_at).where(
                        Campaign.id.in_(campaign_ids)
                    )
                )
            ).all()
        live = [
            due
            for status, due in rows
            if status in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING)
        ]
        if not live:
            return
        due = min((moment or clock.now) for moment in live)
        following = max(due, clock.now + timedelta(seconds=1))
        if following > end:
            # Stop at the horizon without moving past it: a later drive starts here.
            return
        clock.now = following


def sends(meta: FakeMeta, *, exclude: set[str] | None = None) -> list[tuple[datetime, str]]:
    skip = exclude or set()
    return sorted(
        (entry["at"], str(entry["to"])) for entry in meta.requests if str(entry["to"]) not in skip
    )


def max_unique_in_any_window(entries: list[tuple[datetime, str]]) -> int:
    """The most unique recipients in any moving 24 hours, counted at the transport."""
    worst = 0
    for index, (start, _) in enumerate(entries):
        people = {to for at, to in entries[index:] if at < start + DAY}
        worst = max(worst, len(people))
    return worst


def per_window(entries: list[tuple[datetime, str]]) -> list[int]:
    """Unique recipients in consecutive 24-hour windows from the first send."""
    if not entries:
        return []
    first = entries[0][0]
    windows: dict[int, set[str]] = {}
    for at, to in entries:
        windows.setdefault(int((at - first) / DAY), set()).add(to)
    return [len(windows.get(index, set())) for index in range(max(windows) + 1)]


async def recipient_states(database: Database, campaign_id: uuid.UUID) -> Counter[str]:
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(CampaignRecipient.status, CampaignRecipient.attempts).where(
                    CampaignRecipient.campaign_id == campaign_id
                )
            )
        ).all()
    return Counter(f"{status.value}/{attempts}" for status, attempts in rows)


async def campaign(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


async def ledger_rows(database: Database, portfolio_key: str) -> list[BusinessInitiatedSend]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(BusinessInitiatedSend)
                    .join(
                        WhatsAppBusinessPortfolio,
                        WhatsAppBusinessPortfolio.id == BusinessInitiatedSend.portfolio_id,
                    )
                    .where(WhatsAppBusinessPortfolio.meta_key == portfolio_key)
                )
            ).scalars()
        )


def new_waba() -> str:
    return f"waba-limit-{uuid.uuid4().hex[:10]}"


async def start(clock: SimClock) -> None:
    """Stand the simulated clock just after the real moment campaigns were scheduled."""
    clock.stand(datetime.now(UTC) + timedelta(seconds=1))


# --------------------------------------------------------------------------- the numbers


@pytest.mark.parametrize("replicas", [1, 2, 4])
async def test_600_people_on_a_portfolio_of_250_go_200_a_day_for_three_days(
    prepared_database: str, replicas: int
) -> None:
    """The decisive numbers: day 1 exactly 200, day 2 200, day 3 the last 200; nobody twice."""
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    pairs = [worker(prepared_database, meta, name=f"r{index}") for index in range(replicas)]
    database = pairs[0][1]
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=600, waba_id=waba)
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)

        await drive(clock, [replica for replica, _ in pairs], database, [campaign_id], horizon=DAY)
        first_day = sends(meta)
        assert len(first_day) == 200
        row = await campaign(database, campaign_id)
        assert row.status is CampaignStatus.RUNNING
        assert row.waiting_reason == "daily_limit"
        rows = await ledger_rows(database, waba)
        oldest = min(entry.sent_at for entry in rows)
        assert row.resumes_at == oldest + DAY + timedelta(minutes=5)
        assert row.lease_token is None

        await drive(clock, [replica for replica, _ in pairs], database, [campaign_id])

        everything = sends(meta)
        assert per_window(everything) == [200, 200, 200]
        assert max_unique_in_any_window(everything) == 200
        assert meta.received_twice() == 0
        assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
        assert await recipient_states(database, campaign_id) == Counter({"sent/0": 600})
    finally:
        await erase(database)
        for _, each in pairs:
            await each.dispose()


async def test_two_campaigns_on_one_number_share_the_200(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    a, database = worker(prepared_database, meta, name="a")
    b, db_b = worker(prepared_database, meta, name="b")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=600, waba_id=new_waba())
        await read_limit(database, meta, workspace.account_id)
        first = await launch(database, workspace, contact_ids=workspace.contact_ids[:300])
        second = await launch(database, workspace, contact_ids=workspace.contact_ids[300:])
        await start(clock)

        await drive(clock, [a, b], database, [first, second])

        everything = sends(meta)
        assert max_unique_in_any_window(everything) <= 200
        assert per_window(everything) == [200, 200, 200]
        assert meta.received_twice() == 0
        for campaign_id in (first, second):
            assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()


async def test_two_numbers_of_one_portfolio_share_the_200(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    a, database = worker(prepared_database, meta, name="a")
    b, db_b = worker(prepared_database, meta, name="b")
    waba = new_waba()
    try:
        await quiesce(database)
        first_number = await seed(database, recipients=300, waba_id=waba)
        second_number = await add_number(database, first_number, recipients=300, waba_id=waba)
        await read_limit(database, meta, first_number.account_id)
        await read_limit(database, meta, second_number.account_id)
        campaigns = [
            await launch(database, first_number),
            await launch(database, second_number),
        ]
        await start(clock)

        await drive(clock, [a, b], database, campaigns)

        everything = sends(meta)
        assert max_unique_in_any_window(everything) <= 200
        assert per_window(everything) == [200, 200, 200]
        assert meta.received_twice() == 0
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()


async def test_two_workspaces_on_one_portfolio_share_the_200(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    a, database = worker(prepared_database, meta, name="a")
    b, db_b = worker(prepared_database, meta, name="b")
    waba = new_waba()
    try:
        await quiesce(database)
        one = await seed(database, recipients=300, waba_id=waba)
        other = await seed(database, recipients=300, waba_id=waba)
        await read_limit(database, meta, one.account_id)
        await read_limit(database, meta, other.account_id)
        campaigns = [await launch(database, one), await launch(database, other)]
        await start(clock)

        await drive(clock, [a, b], database, campaigns)

        everything = sends(meta)
        assert max_unique_in_any_window(everything) <= 200
        assert per_window(everything) == [200, 200, 200]
        tenants = {row.tenant_id for row in await ledger_rows(database, waba)}
        assert tenants == {one.tenant_id, other.tenant_id}
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()


async def test_follow_ups_and_manual_templates_use_the_window_too(prepared_database: str) -> None:
    """30 people already reached by a follow-up and a person's template: campaigns get 170."""
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="mixed")
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=330, waba_id=waba)
        await read_limit(database, meta, workspace.account_id)
        await start(clock)
        others = workspace.contact_ids[300:]
        async with database.session_factory() as session:
            template = await session.get(WhatsAppTemplate, workspace.template_id)
            assert template is not None
            messaging = MessagingService(
                session=session,
                settings=harness_settings(prepared_database),
                tenant_id=workspace.tenant_id,
                http=meta.client("people"),
            )
            for index, contact_id in enumerate(others):
                conversation = await session.scalar(
                    select(Conversation).where(Conversation.contact_id == contact_id)
                )
                assert conversation is not None
                await messaging.send_template(
                    conversation_id=conversation.id,
                    name=template.name,
                    language=template.language,
                    origin=MessageOrigin.FOLLOW_UP if index % 2 else MessageOrigin.HUMAN,
                    sender_role=TenantRole.TENANT_ADMIN,
                    at=clock.now,
                )
            await session.commit()
        people_before = len(meta.requests)
        assert people_before == 30

        campaign_id = await launch(database, workspace, contact_ids=workspace.contact_ids[:300])
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=12))

        campaign_sends = len(meta.requests) - people_before
        assert campaign_sends == 170
        assert max_unique_in_any_window(sends(meta)) == 200
        sources = Counter(row.source for row in await ledger_rows(database, waba))
        assert sources == Counter({"campaign": 170, "manual_template": 15, "follow_up": 15})
        assert (await campaign(database, campaign_id)).waiting_reason == "daily_limit"
    finally:
        await erase(database)
        await database.dispose()


async def test_people_inside_their_service_window_do_not_use_the_budget(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="open")
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=200, waba_id=waba)
        await start(clock)
        open_ids = await add_contacts(
            database, workspace, 100, last_inbound_at=clock.now - timedelta(hours=1)
        )
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)

        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=12))

        assert len(meta.requests) == 300
        assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
        async with database.session_factory() as session:
            open_numbers = set(
                (
                    await session.execute(select(Contact.wa_id).where(Contact.id.in_(open_ids)))
                ).scalars()
            )
        outside = sends(meta, exclude={str(number) for number in open_numbers})
        assert max_unique_in_any_window(outside) == 200
        assert len({row.recipient for row in await ledger_rows(database, waba)}) == 200
    finally:
        await erase(database)
        await database.dispose()


async def test_a_window_open_on_another_number_does_not_exempt_a_person(
    prepared_database: str,
) -> None:
    """The service window is the conversation's on the sending number, not any of the person's.

    210 people wrote to another number of the workspace an hour ago and to the
    campaign's number two days ago: on the campaign's number their window is
    closed, so they count - 200 on the first day, not 210.
    """
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="elsewhere")
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=210, waba_id=waba)
        other = await add_number(database, workspace, recipients=0, waba_id=new_waba())
        async with database.session_factory() as session:
            for contact_id in workspace.contact_ids:
                session.add(
                    Conversation(
                        tenant_id=workspace.tenant_id,
                        contact_id=contact_id,
                        account_id=other.account_id,
                        status=ConversationStatus.OPEN,
                        mode=ConversationMode.AI,
                        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
                    )
                )
            await session.commit()
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)

        await drive(clock, [replica], database, [campaign_id], horizon=DAY)

        assert len(sends(meta)) == 200
        assert (await campaign(database, campaign_id)).waiting_reason == "daily_limit"
    finally:
        await erase(database)
        await database.dispose()


async def test_an_unlimited_portfolio_is_paced_by_throughput_only(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="unlimited")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=2000, waba_id=new_waba())
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)

        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=2))

        assert per_window(sends(meta)) == [2000]
        assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- changes


def _capability(waba: str, value: str, at: datetime) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba,
                "time": int(at.timestamp()),
                "changes": [
                    {
                        "field": "business_capability_update",
                        "value": {"max_daily_conversations_per_business": value},
                    }
                ],
            }
        ],
    }


async def test_a_raised_limit_is_used_by_the_next_batch(prepared_database: str) -> None:
    """250 -> 2,000 by webhook mid-campaign: the waiting campaign sends the rest at once."""
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="raised")
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=600, waba_id=waba)
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))
        assert len(meta.requests) == 200
        assert (await campaign(database, campaign_id)).waiting_reason == "daily_limit"

        async with database.session_factory() as session:
            await WhatsAppIngestionService(session=session).ingest(
                _capability(waba, "TIER_2K", datetime.now(UTC) + timedelta(minutes=1))
            )
            await session.commit()
        clock.now = clock.now + timedelta(seconds=30)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))

        everything = sends(meta)
        assert per_window(everything) == [600]
        assert max_unique_in_any_window(everything) <= 1600
        assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(database)
        await database.dispose()


async def test_a_lowered_limit_is_used_by_the_next_batch(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="TIER_2K")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="lowered")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=600, waba_id=new_waba())
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)
        await clock.sweep(replica)
        assert len(meta.requests) == 50

        meta.messaging_limit = "TIER_250"
        await read_limit(database, meta, workspace.account_id)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=12))

        assert per_window(sends(meta)) == [200]
        assert (await campaign(database, campaign_id)).waiting_reason == "daily_limit"
    finally:
        await erase(database)
        await database.dispose()


async def test_an_unrecognised_limit_is_250_never_more(prepared_database: str) -> None:
    meta = FakeMeta(messaging_limit="UNTIERED")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="unknown-value")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=300, waba_id=new_waba())
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=12))
        assert per_window(sends(meta)) == [200]
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- the reservation


async def test_a_send_meta_refused_gives_its_place_back_and_an_uncertain_one_keeps_it(
    prepared_database: str,
) -> None:
    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        if n <= 5:
            return meta_error(400, 100)
        if n == 6:
            raise httpx.ReadTimeout("the answer never came")
        return accepted(payload, n)

    meta = FakeMeta(messaging_limit="TIER_250", behaviour=behaviour)
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="release")
    waba = new_waba()
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=20, waba_id=waba)
        await read_limit(database, meta, workspace.account_id)
        campaign_id = await launch(database, workspace)
        await start(clock)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))

        rows = await ledger_rows(database, waba)
        async with database.session_factory() as session:
            statuses = list(
                (
                    await session.execute(
                        select(Message.status).where(
                            Message.id.in_([row.message_id for row in rows])
                        )
                    )
                ).scalars()
            )
        assert len(statuses) == len(rows)
        assert all(status.value != "failed" for status in statuses), "refusals were released"
        assert len(rows) == 20, "14 accepted, 5 refused and later accepted, 1 uncertain"
        states = await recipient_states(database, campaign_id)
        assert states["failed/1"] == 1, "the uncertain one is abandoned, never resent"
        assert states["sent/1"] == 5
        assert meta.received_twice() == 5
    finally:
        await erase(database)
        await database.dispose()


async def test_the_last_place_in_the_window_goes_to_exactly_one_of_two_racing_campaigns(
    prepared_database: str,
) -> None:
    """40 natural races: 199 of 200 places taken; two campaigns, two workers, one person each."""
    meta = FakeMeta(messaging_limit="TIER_250")
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    # One campaign per sweep each, so the two workers take one campaign each.
    a, database = worker(prepared_database, meta, name="a", claim_limit=1)
    b, db_b = worker(prepared_database, meta, name="b", claim_limit=1)
    waba = new_waba()
    try:
        await quiesce(database)
        one = await seed(database, recipients=0, waba_id=waba)
        other = await seed(database, recipients=0, waba_id=waba)
        await read_limit(database, meta, one.account_id)
        await read_limit(database, meta, other.account_id)
        async with database.session_factory() as session:
            portfolio_id = await session.scalar(
                select(WhatsAppAccount.portfolio_id).where(WhatsAppAccount.id == one.account_id)
            )
        wins: list[int] = []
        for _ in range(40):
            async with database.session_factory() as session:
                await session.execute(
                    delete(BusinessInitiatedSend).where(
                        BusinessInitiatedSend.portfolio_id == portfolio_id
                    )
                )
                for index in range(199):
                    session.add(
                        BusinessInitiatedSend(
                            portfolio_id=portfolio_id,
                            recipient=f"filler-{index}",
                            sent_at=datetime.now(UTC),
                            source="follow_up",
                            tenant_id=one.tenant_id,
                        )
                    )
                await session.commit()
            first_person = await add_contacts(
                database, one, 1, last_inbound_at=datetime.now(UTC) - timedelta(days=2)
            )
            second_person = await add_contacts(
                database, other, 1, last_inbound_at=datetime.now(UTC) - timedelta(days=2)
            )
            before = len(meta.requests)
            campaigns = [
                await launch(database, one, contact_ids=first_person),
                await launch(database, other, contact_ids=second_person),
            ]
            await start(clock)
            await asyncio.gather(clock.sweep(a), clock.sweep(b))
            await asyncio.gather(clock.sweep(a), clock.sweep(b))
            wins.append(len(meta.requests) - before)
            async with database.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count(func.distinct(BusinessInitiatedSend.recipient))).where(
                            BusinessInitiatedSend.portfolio_id == portfolio_id
                        )
                    )
                ) == 200
            await quiesce(database, keep=set())
            del campaigns
        assert wins == [1] * 40
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()
