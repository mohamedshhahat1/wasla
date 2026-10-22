"""One marketing message per customer per 24 hours, per workspace (FREQ-01, LIM-08).

Real workers, real commits, the fake Meta at the transport. "One message" is
counted at the transport: requests carrying a marketing template to the
person. The clock moves in the workers only.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.conversation import Contact, Conversation, Message, MessageOrigin
from app.db.models.enums import TenantRole
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.repositories.campaign_repository import AudienceFilter
from app.services.campaign_service import CampaignService
from app.services.contact_cap import ContactFrequencyCapError
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
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
    seed,
    worker,
)

pytestmark = pytest.mark.integration

DAY = timedelta(hours=24)


async def launch(
    database: Database,
    workspace: Workspace,
    *,
    contact_ids: list[uuid.UUID] | None = None,
    send_until: datetime | None = None,
) -> uuid.UUID:
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Cap",
            messages_per_minute=600,
        )
        await service.set_audience(
            campaign_id=campaign.id, filters=AudienceFilter(contact_ids=tuple(contact_ids or ()))
        )
        await session.commit()
        campaign_id = campaign.id
    async with database.session_factory() as session:
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id, send_until=send_until
        )
        await session.commit()
    return campaign_id


async def states(database: Database, campaign_id: uuid.UUID) -> Counter[str]:
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(
                    CampaignRecipient.status,
                    CampaignRecipient.attempts,
                    CampaignRecipient.reason_code,
                ).where(CampaignRecipient.campaign_id == campaign_id)
            )
        ).all()
    return Counter(f"{status.value}/{attempts}/{reason}" for status, attempts, reason in rows)


async def campaign(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


async def drive(
    clock: SimClock,
    replicas: list[Any],
    database: Database,
    ids: list[uuid.UUID],
    *,
    horizon: timedelta,
) -> None:
    end = clock.now + horizon
    for _ in range(400):
        await asyncio.gather(*(clock.sweep(replica) for replica in replicas))
        async with database.session_factory() as session:
            rows = (
                await session.execute(
                    select(Campaign.status, Campaign.next_send_at).where(Campaign.id.in_(ids))
                )
            ).all()
        live = [
            due
            for status, due in rows
            if status in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING)
        ]
        if not live:
            return
        following = max(min(due or clock.now for due in live), clock.now + timedelta(seconds=1))
        if following > end:
            return
        clock.now = following


async def test_two_campaigns_scheduled_a_minute_apart_reach_a_person_once_a_day(
    prepared_database: str,
) -> None:
    """Two workers, two campaigns, the same 20 people: each person once in 24 hours."""
    meta = FakeMeta()
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    a, database = worker(prepared_database, meta, name="a", claim_limit=1)
    b, db_b = worker(prepared_database, meta, name="b", claim_limit=1)
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=20)
        first = await launch(database, workspace)
        await asyncio.sleep(0.01)
        second = await launch(database, workspace)
        clock.now = datetime.now(UTC) + timedelta(seconds=1)

        await drive(clock, [a, b], database, [first, second], horizon=timedelta(hours=12))

        per_person = Counter(str(entry["to"]) for entry in meta.requests)
        assert len(per_person) == 20 and set(per_person.values()) == {1}
        # The two workers interleave: each campaign reached part of the 20, and
        # each waits a day for the people the other reached first.
        sent_now = Counter[str]()
        for campaign_id in (first, second):
            row = await campaign(database, campaign_id)
            tally = await states(database, campaign_id)
            sent_now.update(tally)
            if tally.get("pending/0/None"):
                assert row.status is CampaignStatus.RUNNING
                assert row.waiting_reason == "contact_cap"
        assert sent_now == Counter({"sent/0/None": 20, "pending/0/None": 20})

        await drive(clock, [a, b], database, [first, second], horizon=timedelta(days=2))

        sends = sorted((entry["at"], str(entry["to"])) for entry in meta.requests)
        assert len(sends) == 40
        firsts: dict[str, datetime] = {}
        for at, to in sends:
            if to in firsts:
                assert at - firsts[to] >= DAY, "two marketing messages within 24 hours"
            firsts.setdefault(to, at)
        for campaign_id in (first, second):
            assert await states(database, campaign_id) == Counter({"sent/0/None": 20})
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()


async def test_two_workers_racing_for_a_person_whose_window_is_open_send_once(
    prepared_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap's own statement decides a race no portfolio lock serialises.

    A person whose service window is open is not counted toward Meta's limit,
    so the ledger takes no lock for them (LIM-03): two campaigns on two
    workers reach the cap's claim at once. Both are held at the claim until
    both have arrived; the one conditional UPDATE lets exactly one through.
    """
    meta = FakeMeta()
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    a, database = worker(prepared_database, meta, name="a", claim_limit=1)
    b, db_b = worker(prepared_database, meta, name="b", claim_limit=1)
    arrived = 0
    both = asyncio.Event()
    original = AsyncSession.execute

    async def held_at_the_claim(
        self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal arrived
        if "UPDATE contacts SET last_marketing_sent_at" in str(statement):
            arrived += 1
            if arrived >= 2:
                both.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both.wait(), timeout=10)
        return await original(self, statement, *args, **kwargs)

    try:
        await quiesce(database)
        workspace = await seed(
            database, recipients=1, last_inbound_at=datetime.now(UTC) - timedelta(hours=1)
        )
        first = await launch(database, workspace)
        second = await launch(database, workspace)
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        monkeypatch.setattr(AsyncSession, "execute", held_at_the_claim)

        await asyncio.gather(clock.sweep(a), clock.sweep(b))
        monkeypatch.undo()

        assert arrived == 2, "the two claims did not race"
        assert len(meta.requests) == 1
        tallies = [await states(database, campaign_id) for campaign_id in (first, second)]
        assert sorted(tallies, key=str) == sorted(
            [Counter({"sent/0/None": 1}), Counter({"pending/0/None": 1})], key=str
        )
    finally:
        await erase(database)
        await database.dispose()
        await db_b.dispose()


async def _conversation(database: Database, contact_id: uuid.UUID) -> Conversation:
    async with database.session_factory() as session:
        found = await session.scalar(
            select(Conversation).where(Conversation.contact_id == contact_id)
        )
        assert found is not None
        return found


async def _manual(
    database: Database,
    meta: FakeMeta,
    workspace: Workspace,
    contact_id: uuid.UUID,
    *,
    template_id: uuid.UUID | None = None,
    origin: MessageOrigin = MessageOrigin.HUMAN,
) -> Message:
    conversation = await _conversation(database, contact_id)
    async with database.session_factory() as session:
        template = await session.get(WhatsAppTemplate, template_id or workspace.template_id)
        assert template is not None
        message = await MessagingService(
            session=session,
            settings=harness_settings(database.engine.url.render_as_string(hide_password=False)),
            tenant_id=workspace.tenant_id,
            http=meta.client("person"),
        ).send_template(
            conversation_id=conversation.id,
            name=template.name,
            language=template.language,
            origin=origin,
            sender_role=TenantRole.TENANT_ADMIN,
        )
        await session.commit()
        return message


async def test_a_manual_marketing_template_inside_the_cap_is_refused_with_the_time(
    prepared_database: str,
) -> None:
    meta = FakeMeta()
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=1)
        person = workspace.contact_ids[0]
        await _manual(database, meta, workspace, person)
        with pytest.raises(ContactFrequencyCapError) as refused:
            await _manual(database, meta, workspace, person)
        assert refused.value.status_code == 409
        assert refused.value.error_code == "contact_frequency_cap"
        details = refused.value.details or {}
        available = datetime.fromisoformat(details["available_at"])
        assert DAY - timedelta(minutes=1) < available - datetime.now(UTC) <= DAY
        assert len(meta.requests) == 1
    finally:
        await erase(database)
        await database.dispose()


async def test_utility_templates_and_follow_ups_are_not_capped(prepared_database: str) -> None:
    meta = FakeMeta()
    database = Database(harness_settings(prepared_database))
    try:
        workspace = await seed(database, recipients=1)
        person = workspace.contact_ids[0]
        async with database.session_factory() as session:
            utility = WhatsAppTemplate(
                tenant_id=workspace.tenant_id,
                account_id=workspace.account_id,
                name=f"receipt_{uuid.uuid4().hex[:8]}",
                language="ar_EG",
                category=TemplateCategory.UTILITY,
                status=TemplateStatus.APPROVED,
                body_text="Hello.",
                variable_count=0,
                components=list(BODY_ONLY),
            )
            session.add(utility)
            await session.commit()
            utility_id = utility.id
        await _manual(database, meta, workspace, person)
        await _manual(database, meta, workspace, person, template_id=utility_id)
        await _manual(database, meta, workspace, person, origin=MessageOrigin.FOLLOW_UP)
        assert len(meta.requests) == 3
    finally:
        await erase(database)
        await database.dispose()


async def test_a_send_meta_refused_gives_the_claim_back_and_the_person_gets_it_once(
    prepared_database: str,
) -> None:
    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        return meta_error(400, 100) if n == 1 else accepted(payload, n)

    meta = FakeMeta(behaviour=behaviour)
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="released")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=1)
        campaign_id = await launch(database, workspace)
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))

        assert len(meta.requests) == 2
        assert await states(database, campaign_id) == Counter({"sent/1/None": 1})
        async with database.session_factory() as session:
            contact = await session.get(Contact, workspace.contact_ids[0])
            assert contact is not None and contact.last_marketing_sent_at is not None
            refused = await session.scalar(
                select(Message).where(
                    Message.failure_code == 100, Message.tenant_id == workspace.tenant_id
                )
            )
            assert refused is not None
            assert contact.last_marketing_sent_at != refused.marketing_cap_claimed_at
    finally:
        await erase(database)
        await database.dispose()


async def test_the_cap_and_a_deadline_skip_the_person(prepared_database: str) -> None:
    meta = FakeMeta()
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="capped-deadline")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=3)
        await _manual(database, meta, workspace, workspace.contact_ids[0])
        campaign_id = await launch(
            database, workspace, send_until=datetime.now(UTC) + timedelta(hours=2)
        )
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=3))

        assert await states(database, campaign_id) == Counter(
            {"sent/0/None": 2, "skipped/0/contact_frequency_cap": 1}
        )
        assert (await campaign(database, campaign_id)).status is CampaignStatus.COMPLETED
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- pacing (LIM-08)


async def test_a_held_message_is_sent_and_counted_as_held(prepared_database: str) -> None:
    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        response = accepted(payload, n)
        body = response.json()
        body["messages"][0]["message_status"] = "held_for_quality_assessment"
        return httpx.Response(200, json=body)

    meta = FakeMeta(behaviour=behaviour)
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="held")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=4)
        campaign_id = await launch(database, workspace)
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))

        assert await states(database, campaign_id) == Counter({"sent/0/None": 4})
        async with database.session_factory() as session:
            statistics = await CampaignService(
                session=session, tenant_id=workspace.tenant_id
            ).statistics(campaign_id)
        assert statistics.held == 4 and statistics.sent == 4
    finally:
        await erase(database)
        await database.dispose()


def _failed_status(phone_number_id: str, wamid: str, recipient: str, code: int) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000000",
                                "phone_number_id": phone_number_id,
                            },
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": "failed",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "recipient_id": recipient,
                                    "errors": [{"code": code, "title": "harness"}],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def test_a_held_message_dropped_after_review_pauses_the_campaign(
    prepared_database: str,
) -> None:
    """135000 on a status: Meta is reviewing the portfolio; the campaign pauses, and resumes."""
    meta = FakeMeta()
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="review")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=3)
        campaign_id = await launch(database, workspace)
        async with database.session_factory() as session:
            row = await session.get(Campaign, campaign_id)
            assert row is not None
            row.messages_per_minute = 1
            await session.commit()
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await clock.sweep(replica)
        assert len(meta.requests) == 1
        assert (await campaign(database, campaign_id)).status is CampaignStatus.RUNNING
        async with database.session_factory() as session:
            message = await session.scalar(
                select(Message)
                .join(CampaignRecipient, CampaignRecipient.message_id == Message.id)
                .where(CampaignRecipient.campaign_id == campaign_id)
            )
            assert message is not None and message.wa_message_id is not None
            account = await session.get(WhatsAppAccount, workspace.account_id)
            assert account is not None
            phone_number_id, wamid = account.phone_number_id, message.wa_message_id

        async with database.session_factory() as session:
            await WhatsAppIngestionService(session=session).ingest(
                _failed_status(phone_number_id, wamid, str(meta.requests[0]["to"]), 135000)
            )
            await session.commit()

        reviewed = await campaign(database, campaign_id)
        assert reviewed.status is CampaignStatus.PAUSED
        assert reviewed.status_reason == "meta_quality_review"
        async with database.session_factory() as session:
            failed = await session.get(Message, message.id)
            assert failed is not None and failed.failure_code == 135000
        assert await states(database, campaign_id) == Counter(
            {"sent/0/None": 1, "pending/0/None": 2}
        ), "the others are not failed one by one"

        async with database.session_factory() as session:
            await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
                campaign_id=campaign_id
            )
            await session.commit()
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))
        assert len(meta.requests) == 3
    finally:
        await erase(database)
        await database.dispose()


async def test_meta_s_per_user_marketing_limit_is_final_for_the_person_and_caps_them(
    prepared_database: str,
) -> None:
    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        return meta_error(400, 131049) if n == 1 else accepted(payload, n)

    meta = FakeMeta(behaviour=behaviour)
    clock = SimClock(now=datetime.now(UTC))
    meta.clock = clock.at
    replica, database = worker(prepared_database, meta, name="per-user")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=2)
        campaign_id = await launch(database, workspace)
        clock.now = datetime.now(UTC) + timedelta(seconds=1)
        await drive(clock, [replica], database, [campaign_id], horizon=timedelta(hours=1))

        assert await states(database, campaign_id) == Counter(
            {"sent/0/None": 1, "failed/1/marketing_limit_per_user": 1}
        )
        async with database.session_factory() as session:
            capped = await session.scalar(
                select(Contact)
                .join(CampaignRecipient, CampaignRecipient.contact_id == Contact.id)
                .where(
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.status == RecipientStatus.FAILED,
                )
            )
        assert capped is not None and capped.last_marketing_sent_at is not None
        assert abs(capped.last_marketing_sent_at - clock.now) < timedelta(minutes=1)
    finally:
        await erase(database)
        await database.dispose()
