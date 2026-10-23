"""Follow-ups and manual templates at Meta's limit; quiet hours for follow-ups (FLIM, FQH).

Real `FollowUpWorker` sweeps with their own commits, the real `MessagingService`,
ledger and channel policy; Meta is replaced at the HTTP transport, and "sent"
and "not sent" are counted there, never from Wasla's own rows.

- FLIM-01: a template follow-up (the customer's window closed) at 100 % of the
  portfolio's limit waits for `resumes_at`, spending no attempt and keeping
  `requested_at`; with the limit unknown it is sent and recorded with
  `limit_at_send` null.
- FLIM-02: a person's template at 100 % is refused 409 with when; with the
  limit unknown it is sent with the notice `messaging_limit_unknown`.
- FLIM-03: a free-form follow-up never reaches the ledger.
- FQH-01..04: a template follow-up waits out the workspace's quiet hours; a
  free-form one does not; editing the window or the zone re-evaluates it.

Quiet hours are set around the real now - a window from 90 minutes ago to
9 h 30 min ahead, in whole minutes - because the dispatch reads the real clock;
the margins are hours, never seconds.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.exceptions import ConflictError
from app.core.timezones import zone
from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.conversation import Conversation, MessageOrigin
from app.db.models.enums import TenantRole
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.whatsapp_template import WhatsAppTemplate
from app.db.session import Database
from app.main import create_app
from app.services.campaign_settings_service import CampaignSettingsService
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from app.services.workspace_service import WorkspaceService
from app.workers.follow_up_worker import FollowUpWorker
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import (
    PLATFORM_TOKEN,
    USER_DOMAIN,
    FakeMeta,
    Workspace,
    erase,
    harness_settings,
    read_limit,
    seed,
    worker,
)
from tests.integration.test_manual_marketing_templates import _person

pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit, pytest.mark.quiet_hours]

CAIRO = "Africa/Cairo"


# --------------------------------------------------------------------------- helpers


def follow_up_worker(database_url: str, meta: FakeMeta, database: Database) -> FollowUpWorker:
    settings = harness_settings(database_url)
    http = meta.client("follow-ups")

    def messaging(session: Any, tenant_id: uuid.UUID) -> MessagingService:
        return MessagingService(session=session, settings=settings, tenant_id=tenant_id, http=http)

    return FollowUpWorker(database=database, settings=settings, messaging_factory=messaging)


async def conversations(database: Database, workspace: Workspace) -> list[Conversation]:
    async with database.session_factory() as session:
        rows = await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == workspace.tenant_id)
            .order_by(Conversation.created_at, Conversation.id)
        )
        return list(rows.scalars())


async def template_of(database: Database, workspace: Workspace) -> WhatsAppTemplate:
    async with database.session_factory() as session:
        row = await session.get(WhatsAppTemplate, workspace.template_id)
        assert row is not None
        return row


async def set_quiet_hours(
    database: Database, workspace: Workspace, *, enabled: bool, around_now: bool = True
) -> tuple[time, time]:
    """A window from 90 minutes ago to 9 h 30 min ahead in the workspace's zone, whole minutes."""
    async with database.session_factory() as session:
        tenant = await session.get(Tenant, workspace.tenant_id)
        assert tenant is not None
        now_local = datetime.now(UTC).astimezone(zone(tenant.timezone))
        start = (now_local - timedelta(minutes=90)).time().replace(second=0, microsecond=0)
        end = (now_local + timedelta(hours=9, minutes=30)).time().replace(second=0, microsecond=0)
        if not around_now:
            start, end = time(22), time(9)
        await session.execute(
            update(Tenant)
            .where(Tenant.id == workspace.tenant_id)
            .values(
                campaign_quiet_hours_enabled=enabled,
                campaign_quiet_hours_start=start,
                campaign_quiet_hours_end=end,
            )
        )
        await session.commit()
        return start, end


async def fill_portfolio(
    database: Database,
    database_url: str,
    meta: FakeMeta,
    workspace: Workspace,
    people: list[Conversation],
) -> None:
    """A person's template to each of `people`: the portfolio's whole limit used."""
    template = await template_of(database, workspace)
    async with database.session_factory() as session:
        messaging = MessagingService(
            session=session,
            settings=harness_settings(database_url),
            tenant_id=workspace.tenant_id,
            http=meta.client("people"),
        )
        for conversation in people:
            await messaging.send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
                origin=MessageOrigin.HUMAN,
                sender_role=TenantRole.TENANT_ADMIN,
            )
        await session.commit()


async def schedule_due(
    database: Database,
    workspace: Workspace,
    conversation: Conversation,
    *,
    body: str | None = None,
    template: bool = True,
) -> uuid.UUID:
    """A colleague's follow-up on `conversation`, made due now as if asked for now."""
    row = await template_of(database, workspace)
    async with database.session_factory() as session:
        follow_up = await FollowUpService(session=session, tenant_id=workspace.tenant_id).schedule(
            conversation_id=conversation.id,
            delay=timedelta(minutes=5),
            body=body,
            template_name=row.name if template else None,
            template_language=row.language if template else None,
            created_by_kind=ActorKind.USER,
        )
        moment = datetime.now(UTC) - timedelta(seconds=1)
        follow_up.scheduled_at = moment
        follow_up.requested_at = moment
        await session.commit()
        return follow_up.id


async def row(database: Database, follow_up_id: uuid.UUID) -> FollowUp:
    async with database.session_factory() as session:
        found = await session.get(FollowUp, follow_up_id)
        assert found is not None
        return found


async def ledger_rows(database: Database, workspace: Workspace) -> list[BusinessInitiatedSend]:
    async with database.session_factory() as session:
        rows = await session.execute(
            select(BusinessInitiatedSend).where(
                BusinessInitiatedSend.tenant_id == workspace.tenant_id
            )
        )
        return list(rows.scalars())


def sends_to(meta: FakeMeta, conversation: Conversation, wa_id: str) -> int:
    return sum(1 for entry in meta.requests if entry["to"] == wa_id)


async def wa_id_of(database: Database, conversation: Conversation) -> str:
    from app.db.models.conversation import Contact

    async with database.session_factory() as session:
        contact = await session.get(Contact, conversation.contact_id)
        assert contact is not None and contact.wa_id is not None
        return contact.wa_id


# --------------------------------------------------------------------------- FLIM-01


async def test_a_template_follow_up_at_the_whole_limit_waits_and_spends_nothing(
    prepared_database: str,
) -> None:
    """250 people reached on a TIER_250 portfolio: the 251st, a follow-up, waits for resumes_at."""
    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="flim-wait")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=251)
        await set_quiet_hours(database, workspace, enabled=False)
        await read_limit(database, meta, workspace.account_id)
        people = await conversations(database, workspace)
        await fill_portfolio(database, prepared_database, meta, workspace, people[:250])
        assert len(meta.requests) == 250

        last = people[250]
        follow_up_id = await schedule_due(database, workspace, last)
        asked = (await row(database, follow_up_id)).requested_at

        await sweeper.run_once()

        waiting = await row(database, follow_up_id)
        assert len(meta.requests) == 250, "nothing past the whole limit reaches Meta"
        assert waiting.status is FollowUpStatus.PENDING
        assert waiting.attempts == 0
        assert waiting.deferred_reason == "portfolio_limit"
        assert waiting.requested_at == asked
        assert waiting.scheduled_at > datetime.now(UTC) + timedelta(hours=23)
        assert [entry["reason"] for entry in waiting.deferrals or []] == ["portfolio_limit"]
        assert len(await ledger_rows(database, workspace)) == 250

        # The moment somebody leaves the moving 24 hours: their row ages out.
        async with database.session_factory() as session:
            await session.execute(
                update(BusinessInitiatedSend)
                .where(BusinessInitiatedSend.tenant_id == workspace.tenant_id)
                .values(sent_at=datetime.now(UTC) - timedelta(hours=25))
            )
            await session.execute(
                update(FollowUp)
                .where(FollowUp.id == follow_up_id)
                .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        await sweeper.run_once()

        sent = await row(database, follow_up_id)
        assert sent.status is FollowUpStatus.SENT
        assert sent.attempts == 0
        assert sent.requested_at == asked
        assert sends_to(meta, last, await wa_id_of(database, last)) == 1
        assert len(meta.requests) == 251
    finally:
        await erase(database)
        await database.dispose()


async def test_a_template_follow_up_with_the_limit_unknown_is_sent_and_recorded_without_it(
    prepared_database: str,
) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="flim-unknown")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=1)
        await set_quiet_hours(database, workspace, enabled=False)
        conversation = (await conversations(database, workspace))[0]
        follow_up_id = await schedule_due(database, workspace, conversation)

        await sweeper.run_once()

        assert (await row(database, follow_up_id)).status is FollowUpStatus.SENT
        assert len(meta.requests) == 1
        rows = await ledger_rows(database, workspace)
        assert [(entry.source, entry.limit_at_send) for entry in rows] == [("follow_up", None)]
    finally:
        await erase(database)
        await database.dispose()


async def test_a_free_form_follow_up_at_the_whole_limit_is_sent_and_never_counted(
    prepared_database: str,
) -> None:
    """FLIM-03: inside the customer's window, a follow-up is free-form and costs no budget."""
    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="flim-free")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=251)
        await set_quiet_hours(database, workspace, enabled=True)
        await read_limit(database, meta, workspace.account_id)
        people = await conversations(database, workspace)
        await set_quiet_hours(database, workspace, enabled=False)
        await fill_portfolio(database, prepared_database, meta, workspace, people[:250])
        await set_quiet_hours(database, workspace, enabled=True)
        last = people[250]
        async with database.session_factory() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == last.id)
                .values(last_inbound_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await session.commit()
        follow_up_id = await schedule_due(
            database, workspace, last, body="Still interested?", template=False
        )

        await sweeper.run_once()

        assert (await row(database, follow_up_id)).status is FollowUpStatus.SENT
        assert sends_to(meta, last, await wa_id_of(database, last)) == 1
        assert len(await ledger_rows(database, workspace)) == 250, "no ledger row for free-form"
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- FLIM-02


@pytest_asyncio.fixture
async def inbox(
    prepared_database: str,
    settings: Settings,
    fake_redis: FakeDependency,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FakeMeta, Database, AsyncClient, Settings]]:
    import app.services.messaging_service as messaging_module

    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="flim-manual")
    monkeypatch.setattr(messaging_module, "build_http_client", lambda: meta.client("manual"))
    configured = settings.model_copy(update={"meta_access_token": PLATFORM_TOKEN})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield meta, database, client, configured
    finally:
        await erase(database)
        await database.dispose()


async def test_a_manual_template_at_the_whole_limit_is_refused_with_when(
    prepared_database: str, inbox: tuple[FakeMeta, Database, AsyncClient, Settings]
) -> None:
    meta, database, client, settings = inbox
    workspace = await seed(database, recipients=251)
    await read_limit(database, meta, workspace.account_id)
    people = await conversations(database, workspace)
    await fill_portfolio(database, prepared_database, meta, workspace, people[:250])
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    template = await template_of(database, workspace)

    answer = await client.post(
        f"/api/v1/conversations/{people[250].id}/messages/template",
        json={"name": template.name, "language": template.language},
        headers=admin,
    )

    assert answer.status_code == 409, answer.text
    error = answer.json()["error"]
    assert error["code"] == "portfolio_limit_reached"
    available = datetime.fromisoformat(error["details"]["available_at"])
    available_local = datetime.fromisoformat(error["details"]["available_at_local"])
    assert available == available_local
    assert available_local.utcoffset() == available.astimezone(zone(CAIRO)).utcoffset()
    assert available > datetime.now(UTC) + timedelta(hours=23)
    assert len(meta.requests) == 250
    assert len(await ledger_rows(database, workspace)) == 250


async def test_a_manual_template_with_the_limit_unknown_is_sent_with_a_notice(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings],
) -> None:
    meta, database, client, settings = inbox
    workspace = await seed(database, recipients=1)
    conversation = (await conversations(database, workspace))[0]
    admin = await _person(database, settings, workspace, TenantRole.TENANT_OWNER)
    template = await template_of(database, workspace)

    answer = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages/template",
        json={"name": template.name, "language": template.language},
        headers=admin,
    )

    assert answer.status_code == 201, answer.text
    assert answer.json()["notices"] == ["messaging_limit_unknown"]
    assert len(meta.requests) == 1


async def test_the_manual_template_service_refuses_at_the_limit_before_staging(
    prepared_database: str,
) -> None:
    """The same refusal from the service, with nothing staged and no message row."""
    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="flim-service")
    try:
        workspace = await seed(database, recipients=251)
        await read_limit(database, meta, workspace.account_id)
        people = await conversations(database, workspace)
        await fill_portfolio(database, prepared_database, meta, workspace, people[:250])
        template = await template_of(database, workspace)
        async with database.session_factory() as session:
            messaging = MessagingService(
                session=session,
                settings=harness_settings(prepared_database),
                tenant_id=workspace.tenant_id,
                http=meta.client("service"),
            )
            with pytest.raises(ConflictError) as refused:
                await messaging.send_manual_template(
                    conversation_id=people[250].id,
                    name=template.name,
                    language=template.language,
                    components=None,
                    sent_by_id=None,  # type: ignore[arg-type]
                    sender_role=TenantRole.TENANT_OWNER,
                )
            assert refused.value.error_code == "portfolio_limit_reached"
            from app.db.models.conversation import Message

            staged = await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == people[250].id)
            )
            assert staged == 0
        assert len(meta.requests) == 250
    finally:
        await erase(database)
        await database.dispose()


# --------------------------------------------------------------------------- FQH


async def test_a_template_follow_up_waits_out_quiet_hours_and_a_free_form_one_does_not(
    prepared_database: str,
) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="fqh-night")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=2)
        _, end = await set_quiet_hours(database, workspace, enabled=True)
        # Ordered by id: created in one transaction, they share `created_at`.
        closed, open_window = await conversations(database, workspace)
        async with database.session_factory() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == open_window.id)
                .values(last_inbound_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await session.commit()
        template_id = await schedule_due(database, workspace, closed)
        free_id = await schedule_due(
            database, workspace, open_window, body="Any questions?", template=False
        )
        asked = (await row(database, template_id)).requested_at

        await sweeper.run_once()

        waiting = await row(database, template_id)
        assert waiting.status is FollowUpStatus.PENDING
        assert waiting.deferred_reason == "quiet_hours"
        assert waiting.attempts == 0
        assert waiting.requested_at == asked
        ends = waiting.scheduled_at.astimezone(zone(CAIRO))
        assert (ends.hour, ends.minute) == (end.hour, end.minute)
        assert sends_to(meta, closed, await wa_id_of(database, closed)) == 0
        assert (await row(database, free_id)).status is FollowUpStatus.SENT
        assert sends_to(meta, open_window, await wa_id_of(database, open_window)) == 1
    finally:
        await erase(database)
        await database.dispose()


async def test_switching_quiet_hours_off_releases_a_waiting_follow_up(
    prepared_database: str,
) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="fqh-release")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=1)
        start, end = await set_quiet_hours(database, workspace, enabled=True)
        conversation = (await conversations(database, workspace))[0]
        follow_up_id = await schedule_due(database, workspace, conversation)
        await sweeper.run_once()
        assert (await row(database, follow_up_id)).deferred_reason == "quiet_hours"
        assert len(meta.requests) == 0

        async with database.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            owner = await _owner(database, workspace)
            await CampaignSettingsService(session, tenant=tenant).update(
                actor=owner,
                actor_role=TenantRole.TENANT_OWNER,
                quiet_hours_enabled=False,
                quiet_hours_start=start,
                quiet_hours_end=end,
            )
            await session.commit()
        await sweeper.run_once()

        sent = await row(database, follow_up_id)
        assert sent.status is FollowUpStatus.SENT
        assert sent.attempts == 0
        assert len(meta.requests) == 1
    finally:
        await erase(database)
        await database.dispose()


async def test_a_new_zone_re_evaluates_a_waiting_follow_up(prepared_database: str) -> None:
    """Moved to America/Mexico_City, eight or nine hours behind Cairo: outside the window."""
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="fqh-zone")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=1)
        await set_quiet_hours(database, workspace, enabled=True)
        conversation = (await conversations(database, workspace))[0]
        follow_up_id = await schedule_due(database, workspace, conversation)
        await sweeper.run_once()
        assert (await row(database, follow_up_id)).deferred_reason == "quiet_hours"

        owner = await _owner(database, workspace)
        async with database.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            await WorkspaceService(session).update(
                tenant=tenant,
                actor=owner,
                actor_role=TenantRole.TENANT_OWNER,
                timezone="America/Mexico_City",
            )
            await session.commit()
        await sweeper.run_once()

        assert (await row(database, follow_up_id)).status is FollowUpStatus.SENT
        assert len(meta.requests) == 1
    finally:
        await erase(database)
        await database.dispose()


async def test_a_wait_for_the_night_then_for_the_limit_records_both_in_order(
    prepared_database: str,
) -> None:
    meta = FakeMeta(messaging_limit="TIER_250")
    _, database = worker(prepared_database, meta, name="fqh-both")
    sweeper = follow_up_worker(prepared_database, meta, database)
    try:
        workspace = await seed(database, recipients=251)
        await read_limit(database, meta, workspace.account_id)
        people = await conversations(database, workspace)
        await set_quiet_hours(database, workspace, enabled=False)
        await fill_portfolio(database, prepared_database, meta, workspace, people[:250])
        start, end = await set_quiet_hours(database, workspace, enabled=True)
        follow_up_id = await schedule_due(database, workspace, people[250])
        asked = (await row(database, follow_up_id)).requested_at

        await sweeper.run_once()
        assert (await row(database, follow_up_id)).deferred_reason == "quiet_hours"

        owner = await _owner(database, workspace)
        async with database.session_factory() as session:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            await CampaignSettingsService(session, tenant=tenant).update(
                actor=owner,
                actor_role=TenantRole.TENANT_OWNER,
                quiet_hours_enabled=False,
                quiet_hours_start=start,
                quiet_hours_end=end,
            )
            await session.commit()
        await sweeper.run_once()

        waiting = await row(database, follow_up_id)
        assert [entry["reason"] for entry in waiting.deferrals or []] == [
            "quiet_hours",
            "portfolio_limit",
        ]
        assert waiting.deferred_reason == "portfolio_limit"
        assert waiting.attempts == 0
        assert waiting.requested_at == asked
        assert len(meta.requests) == 250
    finally:
        await erase(database)
        await database.dispose()


async def _owner(database: Database, workspace: Workspace) -> User:
    """An actor for the settings services; the harness erases users of its domain."""
    async with database.session_factory() as session:
        user = User(
            email=f"fqh-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            full_name="Follow-up owner",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()
        return user
