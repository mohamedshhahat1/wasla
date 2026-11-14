"""Each e2e inbox-state helper makes the state it names, read back through the API (EC4-04)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Tenant
from tests.e2e_stack import inbox_states
from tests.integration.test_inbox_contract import (
    LIST,
    Inbox,
    MemoryStorage,
    customer,
    inbox,  # noqa: F401 - the fixture
    message,
)

pytestmark = pytest.mark.integration


class Store(MemoryStorage):
    async def put_at(self, *, key: str, data: bytes, mime_type: str | None = None) -> None:
        self.objects[key] = data


async def test_every_inbox_state_reads_back_as_the_inbox_draws_it(
    inbox: Inbox, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    shop, db = inbox.mine, inbox.database
    monkeypatch.setattr("app.core.storage.build_media_storage", lambda settings: inbox.storage)
    monkeypatch.setattr(inbox.storage, "put_at", Store.put_at.__get__(inbox.storage), raising=False)
    conversation = await customer(
        db,
        shop.tenant_id,
        shop.account_id,
        name="States",
        phone="201055508888",
        last_message_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await message(db, conversation, body="hello")
    target = str(conversation.id)
    async with db.session_factory() as session:
        tenant = await session.get(Tenant, shop.tenant_id)
        assert tenant is not None
        slug = tenant.slug
        made = {
            state: await inbox_states.media(session, target, state)
            for state in inbox_states.MEDIA_STATES
        }
        voice = await inbox_states.voice_note(session, target)
        names = await inbox_states.templates(session, slug)
        # Last: a customer's file reopens the window, as it would.
        closed = await inbox_states.window_closed(session, target)
        await session.commit()
    assert closed["conversation_id"] == target
    assert names == {"utility": "order_update", "marketing": "spring_offer"}

    headers = shop.owner.headers
    read = (await inbox.client.get(f"{LIST}/{target}", headers=headers)).json()
    assert read["reply_policy"]["free_text_allowed"] is False

    items = (await inbox.client.get(f"{LIST}/{target}/messages", headers=headers)).json()["items"]
    by_media = {item["media"][0]["id"]: item["media"][0] for item in items if item["media"]}
    for state, row in made.items():
        assert by_media[row["media_id"]]["status"] == state, state
    note = by_media[voice["media_id"]]
    assert note["status"] == "available" and note["is_voice"] is True
    assert note["transcript_status"] == "ready" and note["transcript"] == inbox_states.TRANSCRIPT
    served = await inbox.client.get(f"{LIST}/{target}/media/{voice['media_id']}", headers=headers)
    assert served.status_code == 200 and served.content == inbox_states.VOICE_BYTES

    templates = (await inbox.client.get("/api/v1/templates", headers=headers)).json()
    listed = templates["templates"]
    assert {"order_update", "spring_offer"} <= {row["name"] for row in listed}
    assert uuid.UUID(str(made["failed"]["message_id"]))
