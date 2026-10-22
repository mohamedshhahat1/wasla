"""A test of a campaign, sent to an admin's own contact (PG-09), over the real API.

The message goes through the real messaging service to the fake Meta at the
transport; what is asserted about it is read from the request Meta received.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ActiveWorkspaceDep, get_media_storage, get_messaging_service
from app.core.dependencies import SessionDep
from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignTestSend,
    OptOutSource,
    RecipientStatus,
)
from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact, Conversation, Message, MessageOrigin
from app.db.models.enums import TenantRole
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.services.messaging_service import MessagingService
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    erase,
    harness_settings,
    quiesce,
    read_limit,
    seed,
    store_file,
    worker,
)

pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit]

BODY = "Hello {{1}}, spring is here."
COMPONENTS: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "IMAGE"},
    {"type": "BODY", "text": BODY},
]
NAME = {"source": "contact.display_name", "fallback": "friend"}


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="tests")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        async with built.session_factory() as session:
            await session.execute(
                CampaignTestSend.__table__.delete().where(  # type: ignore[attr-defined]
                    CampaignTestSend.tenant_id.in_(
                        select(Tenant.id).where(Tenant.slug.startswith("harness-"))
                    )
                )
            )
            await session.commit()
        await erase(built)
        await built.dispose()


@pytest.fixture
def storage(tmp_path: Path) -> MediaStorage:
    return LocalMediaStorage(tmp_path)


def _overrides(database: Database, meta: FakeMeta, storage: MediaStorage) -> dict[Any, Any]:
    settings = harness_settings(database.engine.url.render_as_string(hide_password=False))

    def messaging(session: SessionDep, workspace: ActiveWorkspaceDep) -> MessagingService:
        return MessagingService(
            session=session,
            settings=settings,
            tenant_id=workspace.tenant.id,
            http=meta.client("test-send"),
        )

    return {get_messaging_service: messaging, get_media_storage: lambda: storage}


async def _campaign(
    database: Database, storage: MediaStorage, meta: FakeMeta, *, people: int = 3
) -> tuple[Workspace, str]:
    workspace = await seed(
        database, recipients=people, components=COMPONENTS, body_text=BODY, variable_count=1
    )
    await read_limit(database, meta, workspace.account_id)
    file_id = await store_file(database, storage, workspace)
    async with client_for(database, workspace) as client:
        created = await client.post(
            f"{API}/campaigns",
            json={
                "account_id": str(workspace.account_id),
                "template_id": str(workspace.template_id),
                "name": "Spring",
                "header_media_id": str(file_id),
                "body_parameters": [NAME],
            },
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        assert (
            await client.post(f"{API}/campaigns/{campaign_id}/audience", json={})
        ).status_code == 200
    return workspace, campaign_id


async def _named(database: Database, contact_id: uuid.UUID, name: str) -> None:
    async with database.session_factory() as session:
        await session.execute(
            update(Contact).where(Contact.id == contact_id).values(display_name=name)
        )
        await session.commit()


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_a_test_is_the_campaign_s_message_and_nothing_about_the_campaign_changes(
    database: Database, storage: MediaStorage
) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    workspace, campaign_id = await _campaign(database, storage, meta)
    me = workspace.contact_ids[0]
    await _named(database, me, "Mohamed")
    async with database.session_factory() as session:
        before_cap = await session.scalar(
            select(Contact.last_marketing_sent_at).where(Contact.id == me)
        )

    async with client_for(
        database, workspace, overrides=_overrides(database, meta, storage)
    ) as client:
        sent = await client.post(
            f"{API}/campaigns/{campaign_id}/test-send", json={"contact_id": str(me)}
        )
        assert sent.status_code == 201, sent.text
        statistics = (await client.get(f"{API}/campaigns/{campaign_id}/statistics")).json()

    # One request at Meta, with this contact's own name and the campaign's image.
    (request,) = meta.requests
    components = {c["type"]: c for c in request["payload"]["template"]["components"]}
    assert components["body"]["parameters"][0]["text"] == "Mohamed"
    assert components["header"]["parameters"][0]["type"] == "image"
    assert len(meta.uploads) == 1
    async with database.session_factory() as session:
        message = await session.get(Message, uuid.UUID(sent.json()["id"]))
        assert message is not None and message.origin is MessageOrigin.CAMPAIGN_TEST
        campaign = await session.get(Campaign, uuid.UUID(campaign_id))
        assert campaign is not None
        # The test's upload is never written onto the campaign.
        assert campaign.header_provider_media_id is None
        recipients = await _count(
            session,
            CampaignRecipient,
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.status == RecipientStatus.PENDING,
        )
        assert recipients == 3
        assert (
            await _count(session, CampaignRecipient, CampaignRecipient.message_id == message.id)
            == 0
        )
        record = await session.scalar(
            select(CampaignTestSend).where(CampaignTestSend.campaign_id == campaign.id)
        )
        assert record is not None and record.message_id == message.id
        # Outside the service window: counted in Meta's limit, as a test.
        ledger = await session.scalar(
            select(BusinessInitiatedSend.source).where(
                BusinessInitiatedSend.message_id == message.id
            )
        )
        assert ledger == "campaign_test"
        after_cap = await session.scalar(
            select(Contact.last_marketing_sent_at).where(Contact.id == me)
        )
    assert after_cap == before_cap
    assert statistics["sent"] == 0 and statistics["pending"] == 3


@pytest.mark.quiet_hours
async def test_a_test_ignores_quiet_hours_and_the_contact_cap(
    database: Database, storage: MediaStorage
) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    workspace, campaign_id = await _campaign(database, storage, meta, people=1)
    (me,) = workspace.contact_ids
    async with database.session_factory() as session:
        # Quiet the whole day, and the contact capped a minute ago.
        await session.execute(
            update(Tenant)
            .where(Tenant.id == workspace.tenant_id)
            .values(campaign_quiet_hours_start=time(0, 0), campaign_quiet_hours_end=time(23, 59))
        )
        await session.execute(
            update(Contact)
            .where(Contact.id == me)
            .values(last_marketing_sent_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    async with client_for(
        database, workspace, overrides=_overrides(database, meta, storage)
    ) as client:
        sent = await client.post(
            f"{API}/campaigns/{campaign_id}/test-send", json={"contact_id": str(me)}
        )
    assert sent.status_code == 201, sent.text
    assert len(meta.requests) == 1


async def test_a_workspace_sends_ten_tests_a_day(database: Database, storage: MediaStorage) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    workspace, campaign_id = await _campaign(database, storage, meta, people=1)
    (me,) = workspace.contact_ids
    async with client_for(
        database, workspace, overrides=_overrides(database, meta, storage)
    ) as client:
        for _ in range(10):
            ok = await client.post(
                f"{API}/campaigns/{campaign_id}/test-send", json={"contact_id": str(me)}
            )
            assert ok.status_code == 201, ok.text
        eleventh = await client.post(
            f"{API}/campaigns/{campaign_id}/test-send", json={"contact_id": str(me)}
        )
    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "test_send_limit"
    reset_at = datetime.fromisoformat(eleventh.json()["error"]["details"]["reset_at"])
    assert timedelta(hours=23) < reset_at - datetime.now(UTC) <= timedelta(hours=24)
    assert len(meta.requests) == 10


async def test_who_a_test_may_reach(database: Database, storage: MediaStorage) -> None:
    meta = FakeMeta(messaging_limit="TIER_UNLIMITED")
    workspace, campaign_id = await _campaign(database, storage, meta, people=2)
    opted, other = workspace.contact_ids
    async with database.session_factory() as session:
        session.add(
            ContactChannelConsent(
                tenant_id=workspace.tenant_id,
                contact_id=opted,
                channel=Channel.WHATSAPP,
                marketing_opt_out_at=datetime.now(UTC),
                opt_out_source=OptOutSource.CUSTOMER,
            )
        )
        # A contact of the workspace with no conversation on this number.
        await session.execute(
            Conversation.__table__.delete().where(Conversation.contact_id == other)  # type: ignore[attr-defined]
        )
        await session.commit()
    stranger = await seed(database, recipients=1)
    overrides = _overrides(database, meta, storage)
    async with client_for(database, workspace, overrides=overrides) as client:
        path = f"{API}/campaigns/{campaign_id}/test-send"
        refused = await client.post(path, json={"contact_id": str(opted)})
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "contact_opted_out"
        silent = await client.post(path, json={"contact_id": str(other)})
        assert silent.status_code == 422
        assert silent.json()["error"]["code"] == "contact_has_no_conversation"
        foreign = await client.post(path, json={"contact_id": str(stranger.contact_ids[0])})
        assert foreign.status_code == 404
        await client.post(f"{API}/campaigns/{campaign_id}/cancel")
        cancelled = await client.post(path, json={"contact_id": str(opted)})
        assert cancelled.status_code == 409
        assert cancelled.json()["error"]["code"] == "campaign_not_testable"
    async with client_for(
        database, workspace, role=TenantRole.MEMBER, overrides=overrides
    ) as member:
        assert (await member.post(path, json={"contact_id": str(opted)})).status_code == 403
    assert meta.requests == []


async def test_a_test_is_refused_only_when_the_portfolio_has_nothing_left(
    database: Database, storage: MediaStorage
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    workspace, campaign_id = await _campaign(database, storage, meta, people=1)
    (me,) = workspace.contact_ids
    async with database.session_factory() as session:
        portfolio_id = await session.scalar(
            select(WhatsAppAccount.portfolio_id).where(WhatsAppAccount.id == workspace.account_id)
        )
        # 240 people: past the campaigns' 200, within the whole 250.
        for index in range(240):
            session.add(
                BusinessInitiatedSend(
                    portfolio_id=portfolio_id,
                    recipient=f"filler-{index}",
                    sent_at=datetime.now(UTC),
                    source="follow_up",
                    tenant_id=workspace.tenant_id,
                )
            )
        await session.commit()
    async with client_for(
        database, workspace, overrides=_overrides(database, meta, storage)
    ) as client:
        path = f"{API}/campaigns/{campaign_id}/test-send"
        assert (await client.post(path, json={"contact_id": str(me)})).status_code == 201
        async with database.session_factory() as session:
            for index in range(240, 250):
                session.add(
                    BusinessInitiatedSend(
                        portfolio_id=portfolio_id,
                        recipient=f"filler-{index}",
                        sent_at=datetime.now(UTC),
                        source="follow_up",
                        tenant_id=workspace.tenant_id,
                    )
                )
            await session.commit()
        # The admin was already counted; another person would be the 252nd.
        async with database.session_factory() as session:
            contact = Contact(tenant_id=workspace.tenant_id, wa_id="209999999999")
            session.add(contact)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=workspace.tenant_id,
                    contact_id=contact.id,
                    account_id=workspace.account_id,
                    last_inbound_at=datetime.now(UTC) - timedelta(days=2),
                )
            )
            await session.commit()
            newcomer = contact.id
        full = await client.post(path, json={"contact_id": str(newcomer)})
    assert full.status_code == 409, full.text
    assert full.json()["error"]["code"] == "portfolio_limit_reached"
    assert len(meta.requests) == 1
