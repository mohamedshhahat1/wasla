"""Realtime hints are published after their commit, and carry no content (RT-02, RT-03).

The real ingestion, send, takeover, follow-up and campaign paths, the real ORM
collectors and a real Redis (this stage's own, database 15); each workspace's
stream is read back with XRANGE. A rolled-back write publishes nothing; a
publish that cannot reach Redis never fails the write.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core import events
from app.db.models.conversation import ConversationMode, MessageDirection, MessageOrigin
from app.db.models.enums import TenantRole
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import Database
from app.services.campaign_planner import quiet_hours_of
from app.services.campaign_settings_service import CampaignSettingsService
from app.services.follow_up_service import FollowUpService
from app.services.inbox_service import InboxService
from tests.integration.campaign_harness import (
    USER_DOMAIN,
    FakeMeta,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)
from tests.integration.sequence_harness import (
    ai_reply,
    build,
    conversations,
    customer_writes,
)
from tests.integration.test_follow_up_sequences import _person

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

FORBIDDEN_KEYS = {"body", "text", "name", "phone", "wa_id", "media_id", "parameters"}


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    url = os.environ.get("WASLA_TEST_REDIS_URL")
    if not url:
        pytest.skip("No test Redis: set WASLA_TEST_REDIS_URL.")
    client = Redis.from_url(url, decode_responses=True)
    events.install()
    events.set_event_sink(client, maxlen=5000, retention_seconds=900)
    try:
        yield client
    finally:
        await events.drain()
        events.set_event_sink(None)
        await client.aclose()


async def hints(redis: Redis, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    await events.drain()
    entries = await redis.xrange(events.stream_key(tenant_id))
    return [
        {"id": entry_id, "type": fields["type"], **json.loads(fields["data"])}
        for entry_id, fields in entries
    ]


def scan_for_content(found: list[dict[str, Any]], secrets: list[str]) -> None:
    for hint in found:
        assert not FORBIDDEN_KEYS & set(hint), hint
        blob = json.dumps(hint)
        for secret in secrets:
            assert secret not in blob, (secret, hint)


async def test_an_inbound_message_and_the_reply_are_hinted_without_their_words(
    prepared_database: str, redis: Redis
) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-inbound")
    world = await build(database, prepared_database, meta, steps=[5])
    try:
        conversation = (await conversations(world))[0]
        await customer_writes(world, conversation, "Secret words about my order QX-ZEBRA")
        await ai_reply(world, conversation, "A private answer ZJ-OTTER")

        found = await hints(redis, world.workspace.tenant_id)
        created = [hint for hint in found if hint["type"] == "message.created"]
        assert [hint["direction"] for hint in created] == [
            MessageDirection.INBOUND.value,
            MessageDirection.OUTBOUND.value,
        ]
        assert {hint["conversation_id"] for hint in created} == {str(conversation.id)}
        assert {hint["channel_type"] for hint in created} == {"whatsapp"}
        assert any(hint["type"] == "follow_up.updated" for hint in found), "the run's step"
        scan_for_content(found, ["Secret words", "QX-ZEBRA", "private answer", "ZJ-OTTER"])
    finally:
        await erase(database)
        await database.dispose()


async def test_a_rolled_back_write_publishes_nothing(prepared_database: str, redis: Redis) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-rollback")
    world = await build(database, prepared_database, meta, steps=[5])
    try:
        conversation = (await conversations(world))[0]
        async with database.session_factory() as session:
            await world.messaging(session).send_text(
                conversation_id=conversation.id,
                body="Never committed",
                origin=MessageOrigin.HUMAN,
            )
            await session.rollback()
        # The send commits its own intent (ADR-093); a write rolled back before
        # any commit is a takeover abandoned mid-way:
        async with database.session_factory() as session:
            person = await _person(world)
            await InboxService(session=session, tenant_id=world.workspace.tenant_id).set_mode(
                conversation_id=conversation.id, mode=ConversationMode.HUMAN, actor=person
            )
            await session.flush()
            await session.rollback()

        found = await hints(redis, world.workspace.tenant_id)
        assert not [hint for hint in found if hint["type"] == "conversation.updated"]
    finally:
        await erase(database)
        await database.dispose()


async def test_a_takeover_and_a_follow_up_are_hinted(prepared_database: str, redis: Redis) -> None:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-takeover")
    world = await build(database, prepared_database, meta, steps=[5], enabled=False)
    try:
        conversation = (await conversations(world))[0]
        person = await _person(world)
        async with database.session_factory() as session:
            await FollowUpService(session=session, tenant_id=world.workspace.tenant_id).schedule(
                conversation_id=conversation.id,
                delay=timedelta(minutes=30),
                body="Checking in.",
                created_by_id=person.id,
                created_by_kind=ActorKind.USER,
            )
            await InboxService(session=session, tenant_id=world.workspace.tenant_id).set_mode(
                conversation_id=conversation.id, mode=ConversationMode.HUMAN, actor=person
            )
            await session.commit()

        found = await hints(redis, world.workspace.tenant_id)
        updated = [hint for hint in found if hint["type"] == "conversation.updated"]
        assert updated and "owner" in updated[-1]["fields"]
        assert [hint["state"] for hint in found if hint["type"] == "follow_up.updated"] == [
            "pending"
        ]
        scan_for_content(found, ["Checking in."])
    finally:
        await erase(database)
        await database.dispose()


async def quiet_hours_off(database: Database, tenant_id: uuid.UUID) -> None:
    """Switch the workspace's quiet hours off as its owner does, and check they are off."""
    async with database.session_factory() as session:
        owner = User(
            email=f"rt-owner-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(owner)
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        current = CampaignSettingsService(session, tenant=tenant).read()
        await CampaignSettingsService(session, tenant=tenant).update(
            actor=owner,
            actor_role=TenantRole.TENANT_OWNER,
            quiet_hours_enabled=False,
            quiet_hours_start=current.quiet_hours_start,
            quiet_hours_end=current.quiet_hours_end,
        )
        await session.commit()
        window = await quiet_hours_of(session, tenant_id)
    assert not window.enabled, "quiet hours are off: this test is about hint fan-out, not the night"


async def test_a_campaign_is_hinted_per_transition_never_per_recipient(
    prepared_database: str, redis: Redis
) -> None:
    meta = FakeMeta()
    replica, database = worker(prepared_database, meta, name="rt-campaign")
    try:
        await quiesce(database)
        workspace = await seed(database, recipients=25)
        # Quiet hours would hold every send from 22:00 to 09:00 Cairo (FRM-04);
        # this test is about how many hints a campaign publishes, not the night.
        await quiet_hours_off(database, workspace.tenant_id)
        campaign_id = await compose(database, workspace)
        for _ in range(10):
            if not await replica.run_once():
                break

        found = await hints(redis, workspace.tenant_id)
        changed = [hint for hint in found if hint["type"] == "campaign.changed"]
        assert changed and {hint["campaign_id"] for hint in changed} == {str(campaign_id)}
        assert len(changed) <= 4, "draft -> scheduled -> running -> completed, at most"
        assert len(meta.requests) == 25
    finally:
        await erase(database)
        await database.dispose()


async def test_redis_down_never_fails_the_write(prepared_database: str) -> None:
    events.install()
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    events.set_event_sink(dead)
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-down")
    world = await build(database, prepared_database, meta, steps=[5])
    try:
        conversation = (await conversations(world))[0]
        await customer_writes(world, conversation, "Hello there")
        await events.drain()
        from sqlalchemy import func, select

        from app.db.models.conversation import Message

        async with database.session_factory() as session:
            stored = await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation.id)
            )
        assert stored == 1
    finally:
        events.set_event_sink(None)
        await dead.aclose()
        await erase(database)
        await database.dispose()
