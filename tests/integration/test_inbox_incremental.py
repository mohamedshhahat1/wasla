"""A client catching up reads only what changed (RT-04).

`GET /conversations?updated_since=` lists what changed at or after the moment,
with an ETag on the first page and 304 when nothing changed; a new message
moves a conversation's `updated_at`; `GET /conversations/{id}/messages?after=`
lists only the messages after one the client holds, oldest first. Another
workspace's conversations never appear.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.db.models.conversation import Conversation, Message
from app.db.models.enums import TenantRole
from app.main import create_app
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import PLATFORM_TOKEN, FakeMeta, erase, worker
from tests.integration.sequence_harness import World, ai_reply, build, conversations
from tests.integration.test_manual_marketing_templates import _person

pytestmark = pytest.mark.integration

LIST = "/api/v1/conversations"


@pytest_asyncio.fixture
async def inbox(
    prepared_database: str, settings: Settings, fake_redis: FakeDependency
) -> AsyncIterator[tuple[World, World, AsyncClient, dict[str, str]]]:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="incremental")
    configured = settings.model_copy(update={"meta_access_token": PLATFORM_TOKEN})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    mine = await build(database, prepared_database, meta, steps=[5], recipients=3, enabled=False)
    theirs = await build(database, prepared_database, meta, steps=[5], recipients=1, enabled=False)
    headers = await _person(database, configured, mine.workspace, TenantRole.MEMBER)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield mine, theirs, client, headers
    finally:
        await erase(database)
        await database.dispose()


async def _set_updated(world: World, conversation: Conversation, at: datetime) -> None:
    async with world.database.session_factory() as session:
        # A Core statement naming `updated_at` sets it rather than `now()`.
        await session.execute(
            update(Conversation).where(Conversation.id == conversation.id).values(updated_at=at)
        )
        await session.commit()


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["items"]]


async def test_updated_since_is_inclusive_and_conditional(
    inbox: tuple[World, World, AsyncClient, dict[str, str]],
) -> None:
    world, other, client, headers = inbox
    before, at, after = await conversations(world)
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    await _set_updated(world, before, moment - timedelta(seconds=1))
    await _set_updated(world, at, moment)
    await _set_updated(world, after, moment + timedelta(seconds=1))
    await _set_updated(other, (await conversations(other))[0], moment + timedelta(seconds=2))

    first = await client.get(LIST, params={"updated_since": moment.isoformat()}, headers=headers)
    assert first.status_code == 200, first.text
    assert _ids(first.json()) == [str(at.id), str(after.id)]
    assert first.json()["poll_after_seconds"] == 30
    tag = first.headers["ETag"]

    same = await client.get(
        LIST,
        params={"updated_since": moment.isoformat()},
        headers={**headers, "If-None-Match": tag},
    )
    assert same.status_code == 304

    await ai_reply(world, before)
    changed = await client.get(
        LIST,
        params={"updated_since": moment.isoformat()},
        headers={**headers, "If-None-Match": tag},
    )
    assert changed.status_code == 200
    assert str(before.id) in _ids(changed.json()), "a new message moves updated_at"
    assert changed.headers["ETag"] != tag

    naive = await client.get(LIST, params={"updated_since": "2026-10-30T09:00:00"}, headers=headers)
    assert naive.status_code == 422


async def test_messages_after_one_the_client_holds(
    inbox: tuple[World, World, AsyncClient, dict[str, str]],
) -> None:
    world, other, client, headers = inbox
    conversation = (await conversations(world))[0]
    for text in ("one", "two", "three"):
        await ai_reply(world, conversation, text)
    async with world.database.session_factory() as session:
        held = (
            (
                await session.execute(
                    select(Message.id)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.sequence)
                )
            )
            .scalars()
            .all()
        )

    newer = await client.get(
        f"{LIST}/{conversation.id}/messages", params={"after": str(held[0])}, headers=headers
    )
    assert newer.status_code == 200, newer.text
    assert [item["body"] for item in newer.json()["items"]] == ["two", "three"]
    theirs = (await conversations(other))[0]
    assert (
        await client.get(
            f"{LIST}/{theirs.id}/messages", params={"after": str(held[0])}, headers=headers
        )
    ).status_code == 404
    foreign_anchor = await client.get(
        f"{LIST}/{conversation.id}/messages", params={"after": str(theirs.id)}, headers=headers
    )
    assert foreign_anchor.json()["items"] == []
