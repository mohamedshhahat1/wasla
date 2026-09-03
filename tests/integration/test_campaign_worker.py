"""The campaign worker sweeping a real database.

What matters here is the loop: which campaigns a sweep claims, that one broken
campaign does not strand every other workspace's, and that a paused campaign is
not picked up at all. The sending itself is covered against the service in
`test_campaigns.py`.

The claim is the interesting part. A scheduled campaign whose time has come and
a running one whose rate limit has expired are claimed by the same query,
because the worker does the same thing with both — and splitting them would be
two sweeps racing for the same rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.services.messaging_service import MessagingService
from app.workers.campaign_worker import CampaignWorker
from tests.fakes import as_messaging

pytestmark = pytest.mark.integration


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.opened = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        self.opened += 1
        yield self._session


class StubMessaging:
    """Writes the message row a real send would, and counts the sends."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self.sends = 0

    async def send_template(
        self,
        *,
        conversation_id: uuid.UUID,
        name: str,
        language: str,
        **_: Any,
    ) -> Message:
        self.sends += 1
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEMPLATE,
            status=MessageStatus.SENT,
            template_name=name,
            template_language=language,
        )
        self._session.add(message)
        await self._session.flush()
        return message


async def _sending_campaign(
    session: AsyncSession,
    *,
    slug: str,
    recipients: int = 1,
    status: CampaignStatus = CampaignStatus.SCHEDULED,
    scheduled_at: datetime | None = None,
    template_status: TemplateStatus = TemplateStatus.APPROVED,
) -> Campaign:
    """A campaign the worker's claim should or should not pick up."""
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id=f"waba-{slug}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()

    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name="offer",
        language="ar_EG",
        category=TemplateCategory.MARKETING,
        status=template_status,
        body_text="Hello.",
        variable_count=0,
    )
    session.add(template)
    await session.flush()

    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=account.id,
        template_id=template.id,
        name=slug,
        status=status,
        scheduled_at=scheduled_at or (datetime.now(UTC) - timedelta(minutes=1)),
        audience_size=recipients,
        messages_per_minute=60,
    )
    session.add(campaign)
    await session.flush()

    for index in range(recipients):
        contact = Contact(tenant_id=tenant.id, wa_id=f"20{slug[:4]}{index:06d}")
        session.add(contact)
        await session.flush()
        session.add(
            Conversation(
                tenant_id=tenant.id,
                contact_id=contact.id,
                account_id=account.id,
                status=ConversationStatus.OPEN,
                mode=ConversationMode.AI,
                last_inbound_at=datetime.now(UTC),
            )
        )
        session.add(
            CampaignRecipient(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                contact_id=contact.id,
                status=RecipientStatus.PENDING,
                attempts=0,
            )
        )
    await session.flush()
    return campaign


def _worker(session: AsyncSession, *, sends: list[StubMessaging] | None = None) -> CampaignWorker:
    recorded: list[StubMessaging] = sends if sends is not None else []

    def factory(worker_session: AsyncSession, tenant_id: uuid.UUID) -> MessagingService:
        stub = StubMessaging(worker_session, tenant_id=tenant_id)
        recorded.append(stub)
        return as_messaging(stub)

    return CampaignWorker(
        database=SessionHandle(session),  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        messaging_factory=factory,
    )


async def test_a_scheduled_campaign_whose_time_has_come_is_swept(db_session: AsyncSession) -> None:
    campaign = await _sending_campaign(db_session, slug="due-now", recipients=2)
    sends: list[StubMessaging] = []

    handled = await _worker(db_session, sends=sends).run_once()
    await db_session.flush()

    assert handled == 1
    assert sends[0].sends == 2
    assert campaign.status is CampaignStatus.COMPLETED


async def test_a_campaign_scheduled_for_later_is_left_alone(db_session: AsyncSession) -> None:
    campaign = await _sending_campaign(
        db_session,
        slug="due-later",
        scheduled_at=datetime.now(UTC) + timedelta(hours=2),
    )

    handled = await _worker(db_session).run_once()

    assert handled == 0
    assert campaign.status is CampaignStatus.SCHEDULED


async def test_a_paused_campaign_is_never_claimed(db_session: AsyncSession) -> None:
    """Not merely skipped once claimed: the partial index does not cover it."""
    campaign = await _sending_campaign(db_session, slug="paused", status=CampaignStatus.PAUSED)
    sends: list[StubMessaging] = []

    handled = await _worker(db_session, sends=sends).run_once()

    assert handled == 0
    assert sends == []
    assert campaign.status is CampaignStatus.PAUSED


async def test_a_running_campaign_waits_for_its_rate_limit(db_session: AsyncSession) -> None:
    campaign = await _sending_campaign(db_session, slug="rate-held", status=CampaignStatus.RUNNING)
    campaign.next_send_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.flush()

    assert await _worker(db_session).run_once() == 0


async def test_a_running_campaign_whose_limit_expired_sends_again(db_session: AsyncSession) -> None:
    campaign = await _sending_campaign(db_session, slug="rate-freed", status=CampaignStatus.RUNNING)
    campaign.next_send_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    assert await _worker(db_session).run_once() == 1
    assert campaign.status is CampaignStatus.COMPLETED


async def test_a_cancelled_campaign_is_never_claimed(db_session: AsyncSession) -> None:
    await _sending_campaign(db_session, slug="cancelled", status=CampaignStatus.CANCELLED)

    assert await _worker(db_session).run_once() == 0


async def test_one_broken_campaign_does_not_strand_the_others(db_session: AsyncSession) -> None:
    """Containment: a single bad row must not stop every other workspace."""
    await _sending_campaign(db_session, slug="broken")
    healthy = await _sending_campaign(db_session, slug="healthy")

    calls = {"n": 0}

    def factory(session: AsyncSession, tenant_id: uuid.UUID) -> MessagingService:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("this workspace is broken")
        return as_messaging(StubMessaging(session, tenant_id=tenant_id))

    worker = CampaignWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        messaging_factory=factory,
    )

    handled = await worker.run_once()
    await db_session.flush()

    assert handled == 1
    assert healthy.status is CampaignStatus.COMPLETED


async def test_an_idle_sweep_opens_one_session_and_returns(db_session: AsyncSession) -> None:
    handle = SessionHandle(db_session)
    worker = CampaignWorker(
        database=handle,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        messaging_factory=lambda session, tenant_id: as_messaging(
            StubMessaging(session, tenant_id=tenant_id)
        ),
    )

    assert await worker.run_once() == 0
    assert handle.opened == 1


async def test_a_sweep_crosses_workspaces_but_each_campaign_stays_in_its_own(
    db_session: AsyncSession,
) -> None:
    first = await _sending_campaign(db_session, slug="tenant-one")
    second = await _sending_campaign(db_session, slug="tenant-two")
    sends: list[StubMessaging] = []

    handled = await _worker(db_session, sends=sends).run_once()
    await db_session.flush()

    assert handled == 2
    assert first.tenant_id != second.tenant_id
    # One messaging service per campaign, each built from that row's own tenant.
    assert {stub._tenant_id for stub in sends} == {first.tenant_id, second.tenant_id}


async def test_stopping_wakes_a_sleeping_worker(db_session: AsyncSession) -> None:
    """Shutdown must not wait out a full poll interval."""
    worker = CampaignWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        poll_seconds=60.0,
        messaging_factory=lambda session, tenant_id: as_messaging(
            StubMessaging(session, tenant_id=tenant_id)
        ),
    )
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)

    worker.stop()
    await asyncio.wait_for(task, timeout=2)
