"""What a person sees of a conversation's follow-ups, and the stop button (FUS-09).

Through the real ASGI application with real access tokens: the conversation's
follow-up state on `GET /conversations/{id}`, its history on
`GET /conversations/{id}/follow-ups` (the text of a sent step only), and
`DELETE /conversations/{id}/follow-up` stopping the run, audited. Another
workspace's conversation is not found on any of the three.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.enums import TenantRole
from app.main import create_app
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import PLATFORM_TOKEN, FakeMeta, erase, worker
from tests.integration.sequence_harness import (
    STEP_PREFIX,
    World,
    ai_reply,
    build,
    conversations,
    make_due,
    step_sends,
    sweeper,
    wa_id,
)
from tests.integration.test_manual_marketing_templates import _person

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]


@pytest_asyncio.fixture
async def inbox(
    prepared_database: str,
    settings: Settings,
    fake_redis: FakeDependency,
) -> AsyncIterator[tuple[World, World, AsyncClient, Settings]]:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="visibility")
    configured = settings.model_copy(update={"meta_access_token": PLATFORM_TOKEN})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    mine = await build(database, prepared_database, meta, steps=[5, 30])
    theirs = await build(database, prepared_database, meta, steps=[5])
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield mine, theirs, client, configured
    finally:
        await erase(database)
        await database.dispose()


def _url(conversation: Any, tail: str = "") -> str:
    return f"/api/v1/conversations/{conversation.id}{tail}"


async def test_the_state_the_history_and_the_stop_button(
    inbox: tuple[World, World, AsyncClient, Settings],
) -> None:
    world, _, client, settings = inbox
    member = await _person(world.database, settings, world.workspace, TenantRole.MEMBER)
    conversation = (await conversations(world))[0]
    number = await wa_id(world, conversation)

    before = await client.get(_url(conversation), headers=member)
    assert before.status_code == 200, before.text
    assert before.json()["follow_up"]["state"] == "none"

    await ai_reply(world, conversation)
    shown = (await client.get(_url(conversation), headers=member)).json()["follow_up"]
    assert (shown["state"], shown["step_index"], shown["steps_total"]) == ("scheduled", 1, 2)
    assert shown["due_at_local"].endswith(("+02:00", "+03:00"))

    await make_due(world, conversation)
    await sweeper(world).run_once()
    history = await client.get(_url(conversation, "/follow-ups"), headers=member)
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    # Latest due time first, as `GET /follow-ups` lists them.
    assert [(item["step_index"], item["status"]) for item in items] == [
        (2, "pending"),
        (1, "sent"),
    ]
    assert items[1]["text"].startswith(STEP_PREFIX)
    assert items[0]["text"] is None

    stopped = await client.delete(_url(conversation, "/follow-up"), headers=member)
    assert stopped.status_code == 200, stopped.text
    assert (stopped.json()["state"], stopped.json()["ended_reason"]) == (
        "ended",
        "stopped_by_person",
    )
    async with world.database.session_factory() as session:
        trail = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.FOLLOW_UP_STOPPED_BY_PERSON,
                        AuditLog.target_id == conversation.id,
                    )
                )
            ).scalars()
        )
    assert len(trail) == 1
    items = (await client.get(_url(conversation, "/follow-ups"), headers=member)).json()["items"]
    assert items[0]["cancelled_reason"] == "stopped_by_person"
    await make_due(world, conversation)
    await sweeper(world).run_once()
    assert len(step_sends(world, number)) == 1

    again = await client.delete(_url(conversation, "/follow-up"), headers=member)
    assert again.status_code == 200 and again.json()["state"] == "ended"


async def test_a_skipped_step_shows_its_reason_and_never_its_text(
    inbox: tuple[World, World, AsyncClient, Settings],
) -> None:
    world, _, client, settings = inbox
    member = await _person(world.database, settings, world.workspace, TenantRole.MEMBER)
    conversation = (await conversations(world))[0]
    await ai_reply(world, conversation)
    await make_due(world, conversation)
    world.model.answer = lambda payload: {"action": "skip", "reason": "concluded", "text": "bye"}
    await sweeper(world).run_once()

    (item,) = (await client.get(_url(conversation, "/follow-ups"), headers=member)).json()["items"]
    assert (item["status"], item["skip_reason"], item["text"]) == ("skipped", "concluded", None)
    state = (await client.get(_url(conversation), headers=member)).json()["follow_up"]
    assert (state["state"], state["ended_reason"]) == ("ended", "ai_skipped")


async def test_another_workspace_s_conversation_is_not_found(
    inbox: tuple[World, World, AsyncClient, Settings],
) -> None:
    world, other, client, settings = inbox
    member = await _person(world.database, settings, world.workspace, TenantRole.MEMBER)
    theirs = (await conversations(other))[0]
    await ai_reply(other, theirs)

    assert (await client.get(_url(theirs), headers=member)).status_code == 404
    assert (await client.get(_url(theirs, "/follow-ups"), headers=member)).status_code == 404
    assert (await client.delete(_url(theirs, "/follow-up"), headers=member)).status_code == 404
    async with other.database.session_factory() as session:
        from app.db.models.follow_up_sequence import FollowUpSequenceRun

        run = await session.scalar(
            select(FollowUpSequenceRun).where(FollowUpSequenceRun.conversation_id == theirs.id)
        )
        assert run is not None and run.state == "active"
