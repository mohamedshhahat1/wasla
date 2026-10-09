"""Sweeps wait on a throttle and stop on a broken connection (OMNI-035).

Classified by HTTP status, Meta's 130429 on a 400 was a per-message decline - a
campaign marked each recipient failed instead of backing off - and a revoked
permission (code 10) burned every recipient's attempt one at a time with the
connection still reading healthy. Each case answers through a fake Meta endpoint
with **HTTP 400** and Meta's documented code, and drives the real campaign and
follow-up services, the messaging choke point and the WhatsApp client.

ADR-093 is untouched: a throttle and a connection refusal are both declines
before reading, so the staged message is recorded undelivered - never left
`requested` - and nothing is ever sent twice.

Mutants this suite kills: M-O12 at the sweep (a throttle filed as a recipient
failure) and M-O13 (a connection-level code treated as per-message).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.outcomes import ProviderConnectionRefusedError
from app.core.config import Settings
from app.db.models.campaign import CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.channel import ChannelConnection, ConnectionHealth
from app.db.models.conversation import (
    Message,
    MessageDeliveryState,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.services import messaging_service as messaging_module
from app.services.campaign_service import CampaignService
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from tests.integration.test_omnichannel_operations import (
    _campaign,
    _customer,
    _number,
    _tenant,
)

pytestmark = pytest.mark.integration


class RefusingMeta:
    """Meta's messages endpoint answering 400 with one documented code, or success."""

    def __init__(self) -> None:
        self.code: int | None = 130429
        self.requests = 0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.code is None:
                return httpx.Response(200, json={"messages": [{"id": f"wamid.{uuid.uuid4().hex}"}]})
            body = {
                "error": {"message": "(Meta's words)", "type": "OAuthException", "code": self.code}
            }
            return httpx.Response(400, content=json.dumps(body).encode())

        return httpx.MockTransport(handle)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
    )


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[RefusingMeta]:
    fake = RefusingMeta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


async def _health(session: AsyncSession, connection_id: uuid.UUID) -> ConnectionHealth:
    health: ConnectionHealth | None = await session.scalar(
        select(ChannelConnection.health)
        .where(ChannelConnection.id == connection_id)
        .execution_options(populate_existing=True)
    )
    assert health is not None
    return health


async def _campaign_with_two(
    session: AsyncSession, settings: Settings
) -> tuple[CampaignService, Any, list[CampaignRecipient], uuid.UUID]:
    tenant = await _tenant(session)
    account = await _number(session, tenant)
    campaign = await _campaign(session, tenant, account)
    recipients = []
    for phone in ("201000000551", "201000000552"):
        contact, conversation = await _customer(session, tenant, account, phone)
        recipient = CampaignRecipient(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            conversation_id=conversation.id,
        )
        session.add(recipient)
        recipients.append(recipient)
    await session.flush()
    service = CampaignService(
        session=session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=session, settings=settings, tenant_id=tenant.id),
    )
    return service, campaign, recipients, account.id


async def test_a_throttling_code_makes_a_campaign_wait_and_fails_nobody(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    service, campaign, recipients, connection_id = await _campaign_with_two(db_session, settings)
    now = datetime.now(UTC)

    outcome = await service.dispatch_batch(campaign, now=now)

    assert (outcome.sent, outcome.failed, outcome.skipped) == (0, 0, 0)
    for recipient in recipients:
        await db_session.refresh(recipient)
        assert recipient.status is RecipientStatus.PENDING
        assert recipient.attempts == 0
        # Unlinked from the throttled send, so the next batch sends afresh
        # rather than abandoning it as "could not be confirmed".
        assert recipient.message_id is None
    assert campaign.status is CampaignStatus.RUNNING
    assert campaign.next_send_at is not None
    assert campaign.next_send_at >= now + timedelta(seconds=50)
    assert await _health(db_session, connection_id) is ConnectionHealth.RATE_LIMITED
    # The throttled request was retried, then recorded as undelivered.
    (staged,) = (
        await db_session.scalars(select(Message).where(Message.tenant_id == campaign.tenant_id))
    ).all()
    assert staged.delivery_state is MessageDeliveryState.UNDELIVERED
    assert meta.requests == 3

    # The throttle passes: the same recipients are sent, once each, and the
    # connection is healthy again.
    meta.code = None
    later = await service.dispatch_batch(campaign, now=campaign.next_send_at)
    assert later.sent == 2
    assert await _health(db_session, connection_id) is ConnectionHealth.OK


async def test_a_connection_level_code_stops_the_campaign_after_one_recipient(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    meta.code = 10
    service, campaign, recipients, connection_id = await _campaign_with_two(db_session, settings)

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert outcome.status is CampaignStatus.FAILED
    assert meta.requests == 1, "the second recipient was never tried"
    await db_session.refresh(recipients[1])
    assert recipients[1].attempts == 0
    assert recipients[1].status is RecipientStatus.PENDING
    assert await _health(db_session, connection_id) is ConnectionHealth.PERMISSION_MISSING


@pytest.mark.parametrize("code", [131031, 133010])
async def test_an_account_restriction_or_unregistered_number_also_stops_it(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta, code: int
) -> None:
    meta.code = code
    service, campaign, _, connection_id = await _campaign_with_two(db_session, settings)

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert outcome.status is CampaignStatus.FAILED
    assert meta.requests == 1
    assert await _health(db_session, connection_id) is ConnectionHealth.PERMISSION_MISSING


async def test_an_ordinary_decline_is_still_this_recipients(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    meta.code = 131047
    service, campaign, recipients, connection_id = await _campaign_with_two(db_session, settings)

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert outcome.status is CampaignStatus.RUNNING
    assert meta.requests == 2
    for recipient in recipients:
        await db_session.refresh(recipient)
        assert recipient.attempts == 1
    assert await _health(db_session, connection_id) is ConnectionHealth.OK


async def _follow_up(
    session: AsyncSession, settings: Settings
) -> tuple[FollowUpService, FollowUp, uuid.UUID]:
    tenant = await _tenant(session)
    account = await _number(session, tenant)
    _, conversation = await _customer(session, tenant, account, "201000000561")
    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        status=FollowUpStatus.PENDING,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        body="Just checking in.",
        created_by_kind="user",
    )
    session.add(follow_up)
    await session.flush()
    service = FollowUpService(
        session=session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=session, settings=settings, tenant_id=tenant.id),
    )
    return service, follow_up, account.id


async def test_a_throttled_follow_up_waits_and_spends_no_attempt(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    service, follow_up, connection_id = await _follow_up(db_session, settings)

    result = await service.dispatch(follow_up)

    assert result.follow_up.status is FollowUpStatus.PENDING
    assert result.follow_up.attempts == 0
    assert result.follow_up.message_id is None
    assert result.follow_up.scheduled_at >= datetime.now(UTC) + timedelta(seconds=50)
    assert await _health(db_session, connection_id) is ConnectionHealth.RATE_LIMITED


async def test_a_connection_refusal_stops_the_follow_up_sweep(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    meta.code = 10
    service, follow_up, connection_id = await _follow_up(db_session, settings)

    with pytest.raises(ProviderConnectionRefusedError):
        await service.dispatch(follow_up)

    assert await _health(db_session, connection_id) is ConnectionHealth.PERMISSION_MISSING


async def test_a_person_sending_into_a_throttle_gets_the_undelivered_message_back(
    db_session: AsyncSession, settings: Settings, meta: RefusingMeta
) -> None:
    """Not a sweep: nothing is raised at them, the row says what happened."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000571")
    messaging = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await messaging.send_text(
        conversation_id=conversation.id, body="Hello", origin=MessageOrigin.HUMAN
    )

    assert message.status is MessageStatus.FAILED
    assert message.delivery_state is MessageDeliveryState.UNDELIVERED
    assert message.failure_reason == "The provider is rate limiting this connection."
    assert await _health(db_session, account.id) is ConnectionHealth.RATE_LIMITED
