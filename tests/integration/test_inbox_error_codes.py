"""The inbox errors FE-4's screens must tell apart carry their own codes (FEB-06, EC4).

Each keeps its status and its sentence; only `error_code` is new, and no body
names another member or workspace (EC4-03).
"""

# ruff: noqa: F811 - `desk` is a fixture imported above and taken by name
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.test_conversation_read_state import (
    desk,  # noqa: F401 - the fixture
    send,
    stored_conversation,
    writer,
)
from tests.integration.test_inbox_contract import LIST, Inbox, message

pytestmark = pytest.mark.integration


async def test_a_send_after_the_window_closed_has_its_own_code(desk: Inbox) -> None:
    """EC4-02: reply_window_closed - same status and sentence, its own code."""
    conversation = await writer(desk, last_message_at=datetime.now(UTC) - timedelta(hours=25))
    refused = await send(desk, conversation, desk.mine.member.headers)
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "reply_window_closed"
    assert "24" in error["message"] or "window" in error["message"].lower()
    stored = await stored_conversation(desk.database, conversation.id)
    assert stored.assigned_to_id is None


async def test_a_key_reused_for_other_words_has_its_own_code(desk: Inbox) -> None:
    """EC4-02: idempotency_key_reused - still 409, the same sentence."""
    conversation = await writer(desk)
    await message(desk.database, conversation, body="hi")
    key = str(uuid.uuid4())
    first = await send(desk, conversation, desk.mine.owner.headers, key=key, body="one")
    assert first.status_code == 201
    reused = await send(desk, conversation, desk.mine.owner.headers, key=key, body="two")
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "idempotency_key_reused"
    assert reused.json()["error"]["message"] == (
        "That idempotency key was already used for a different message."
    )


async def test_a_refused_file_type_has_its_own_code(desk: Inbox) -> None:
    """EC4-02: media_type_not_allowed - the bytes are no type Wasla accepts."""
    conversation = await writer(desk)
    await message(desk.database, conversation, body="hi")
    refused = await desk.client.post(
        f"{LIST}/{conversation.id}/messages/media",
        files={"file": ("notes.exe", b"MZ\x90\x00 not a document", "application/pdf")},
        headers={**desk.mine.owner.headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "media_type_not_allowed"


async def test_a_stale_assignment_keeps_its_code_and_names_nobody(
    desk: Inbox,
) -> None:
    """EC4-03: the body says the owner changed, never who has it now."""
    shop = desk.mine
    conversation = await writer(desk, assigned_to_id=shop.admin.user_id)
    refused = await desk.client.post(
        f"{LIST}/{conversation.id}/assignment",
        json={"assigned_to_id": str(shop.member.user_id), "expected_assigned_to_id": None},
        headers=shop.member.headers,
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "stale_assignment"
    assert str(shop.admin.user_id) not in refused.text
