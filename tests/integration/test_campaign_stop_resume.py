"""Pause means pause; a send that provably never left is sent on resume; a stuck sweep stops.

CMP-08, CMP-11, CMP-20 and the MSG-22 regression the audit found missing.
Ported from the audit's P03 (`pause`, `cancel`) and P10 (credential refused,
then resumed): pause let the whole claimed batch go (30 of 30), and the
person in flight at a refused credential was abandoned on resume (4 of 5
received it). Counted at the Meta transport.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    CampaignStopReason,
    RecipientReason,
)
from app.db.models.conversation import (
    Conversation,
    MessageDeliveryState,
    MessageKind,
    MessageOrigin,
)
from app.db.session import Database
from app.repositories.conversation_repository import MessageRepository
from app.services.campaign_service import CampaignService
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    accepted,
    compose,
    erase,
    ledger,
    meta_error,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def world(prepared_database: str) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="stop")
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def _campaign(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


@pytest.mark.parametrize(
    ("action", "status"),
    [("pause", CampaignStatus.PAUSED), ("cancel", CampaignStatus.CANCELLED)],
)
async def test_a_pause_or_cancel_mid_batch_stops_at_the_next_recipient(
    world: tuple[FakeMeta, CampaignWorker, Database], action: str, status: CampaignStatus
) -> None:
    """P03: committed when Meta has seen 5 of 30 - audit: pause 30 requests, cancel 5.

    Cancel is the MSG-22 regression the audit found without a test; pause is
    CMP-08. Both are the worker's per-recipient `hold`.
    """
    meta, each, database = world
    workspace = await seed(database, recipients=30)
    campaign_id = await compose(database, workspace)

    async def on_request(payload: dict[str, Any], n: int) -> None:
        if n == 5:
            async with database.session_factory() as session:
                service = CampaignService(session=session, tenant_id=workspace.tenant_id)
                await getattr(service, action)(campaign_id)
                await session.commit()

    meta.on_request = on_request
    await each.run_once()

    # The fifth was in flight when the stop committed; nothing after it went.
    assert len(meta.requests) <= 6
    row = await ledger(database, campaign_id)
    assert row.status is status
    assert row.recipients["pending/attempts=0"] >= 24
    assert row.meters == row.sent == len(meta.requests)


async def test_a_send_refused_by_the_credential_is_sent_on_resume(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """P10: 401 on the third send, reconnect, reschedule - audit: 4 of 5 received it.

    The third person's message is UNDELIVERED - Meta refused the credential
    before reading it - so the resumed campaign re-arms them (CMP-11, ADR-140)
    rather than abandoning them as "could not be confirmed".
    """
    meta, each, database = world
    delivered: Counter[str] = Counter()
    refuse = {"on": True}

    async def answer(payload: dict[str, Any], n: int) -> httpx.Response:
        if n == 3 and refuse["on"]:
            return meta_error(401, None)
        delivered[str(payload.get("to"))] += 1
        return accepted(payload, n)

    meta.behaviour = answer
    workspace = await seed(database, recipients=5)
    campaign_id = await compose(database, workspace)

    await each.run_once()
    failed = await _campaign(database, campaign_id)
    assert failed.status is CampaignStatus.FAILED
    assert failed.status_reason == CampaignStopReason.CREDENTIAL_REFUSED

    refuse["on"] = False
    async with database.session_factory() as session:
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id
        )
        await session.commit()
    await each.run_once(now=datetime.now(UTC) + timedelta(seconds=1))

    assert len(delivered) == 5
    assert max(delivered.values()) == 1
    row = await ledger(database, campaign_id)
    assert dict(row.recipients) == {"sent/attempts=0": 5}
    assert row.status is CampaignStatus.COMPLETED


async def _requested(database: Database, workspace: Workspace, campaign_id: uuid.UUID) -> str:
    """Leave one recipient as a crash between TX1/REQUESTED and Meta's answer would."""
    async with database.session_factory() as session:
        recipient = (
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
        conversation = (
            (
                await session.execute(
                    select(Conversation).where(
                        Conversation.contact_id == recipient.contact_id,
                        Conversation.account_id == workspace.account_id,
                    )
                )
            )
            .scalars()
            .one()
        )
        message = await MessageRepository(session, tenant_id=workspace.tenant_id).stage_outbound(
            conversation_id=conversation.id,
            kind=MessageKind.TEMPLATE,
            body=None,
            sent_by_id=None,
            template_name="harness",
            template_language="ar_EG",
            origin=MessageOrigin.CAMPAIGN,
        )
        await session.flush()
        message.delivery_state = MessageDeliveryState.REQUESTED
        recipient.message_id = message.id
        recipient.conversation_id = conversation.id
        await session.commit()
        return str(recipient.id)


async def test_a_send_that_may_have_left_is_never_re_armed(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """The ADR-093 side of the re-arm rule: pending and linked to a REQUESTED message.

    Whether Meta took it is unknowable, so the recipient is abandoned - never
    sent again - whatever the campaign's own state says (kills M-C25).
    """
    meta, each, database = world
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)
    abandoned = await _requested(database, workspace, campaign_id)

    await each.run_once()

    assert len(meta.requests) == 2
    async with database.session_factory() as session:
        row = await session.get(CampaignRecipient, uuid.UUID(abandoned))
        assert row is not None
        assert row.reason_code == RecipientReason.DELIVERY_UNCONFIRMED
        assert row.message_id is not None


async def test_a_batch_that_keeps_raising_is_paused_with_a_reason(
    world: tuple[FakeMeta, CampaignWorker, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CMP-20: audit - retried every five minutes for ever, with no `last_error`."""
    meta, each, database = world
    workspace = await seed(database, recipients=2)
    campaign_id = await compose(database, workspace)

    async def broken(self: CampaignService, campaign: Campaign) -> bool:
        raise RuntimeError("harness: a batch that cannot run")

    monkeypatch.setattr(CampaignService, "_channel_in_plan", broken)
    start = datetime.now(UTC)
    for index in range(4):
        await each.run_once(now=start + timedelta(minutes=10 * index))
        row = await _campaign(database, campaign_id)
        assert row.consecutive_batch_failures == index + 1
        assert row.last_error and "could not run" in row.last_error
        assert row.status is not CampaignStatus.PAUSED

    await each.run_once(now=start + timedelta(minutes=50))

    row = await _campaign(database, campaign_id)
    assert row.status is CampaignStatus.PAUSED
    assert row.status_reason == CampaignStopReason.REPEATED_BATCH_FAILURE
    assert len(meta.requests) == 0


async def test_a_batch_that_works_again_resets_the_failure_count(
    world: tuple[FakeMeta, CampaignWorker, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, each, database = world
    workspace = await seed(database, recipients=2)
    campaign_id = await compose(database, workspace, rate=1)
    original = CampaignService._channel_in_plan
    calls = {"n": 0}

    async def flaky(self: CampaignService, campaign: Campaign) -> bool:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("harness")
        return await original(self, campaign)

    monkeypatch.setattr(CampaignService, "_channel_in_plan", flaky)
    start = datetime.now(UTC)
    for index in range(3):
        await each.run_once(now=start + timedelta(minutes=10 * index))

    row = await _campaign(database, campaign_id)
    assert row.consecutive_batch_failures == 0
    assert len(meta.requests) == 1


async def test_pacing_counts_from_when_the_batch_ended(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """CMP-20: `next_send_at` was dated from the sweep's start, so a long batch overshot.

    Five sends at 0.2 s each take a second; at 600 a minute the next five may
    follow half a second after the batch ended, not after it began.
    """
    meta, each, database = world
    meta.latency = 0.2
    workspace = await seed(database, recipients=10)
    campaign_id = await compose(database, workspace, rate=600)
    async with database.session_factory() as session:
        await session.execute(
            update(Campaign).where(Campaign.id == campaign_id).values(messages_per_minute=5)
        )
        await session.commit()

    start = datetime.now(UTC)
    await each.run_once(now=start)

    row = await _campaign(database, campaign_id)
    assert row.next_send_at is not None
    # Five sends took about a second; the minute the rate grants starts after them.
    assert row.next_send_at - start >= timedelta(minutes=1, seconds=0.9)
