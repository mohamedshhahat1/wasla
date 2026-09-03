"""The conversation collection endpoints.

These cover the HTTP contract of the paged collections: the envelope shape, that
the cursor reaches the service rather than being quietly dropped, and that a
cursor the caller invented is a 422 rather than a 500. The paging behaviour
itself is proved against PostgreSQL in `test_pagination.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_inbox_service,
    get_messaging_service,
    get_sentiment_service,
)
from app.core.pagination import MAX_CURSOR_LENGTH, Cursor, Page
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.sentiment import ConversationPriority

pytestmark = pytest.mark.integration

PATH = "/api/v1/conversations"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONVERSATION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CONTACT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
ACCOUNT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
MESSAGE_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
MOMENT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
NEXT_CURSOR = Cursor(sort_value=MOMENT, id=CONVERSATION_ID).encode()


def _conversation() -> Conversation:
    return Conversation(
        id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        contact_id=CONTACT_ID,
        account_id=ACCOUNT_ID,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
        # Set explicitly, like `mode` and `status` above: a column default is
        # applied at insert, and this row is never inserted.
        priority=ConversationPriority.NORMAL,
        last_message_at=MOMENT,
        last_inbound_at=MOMENT,
        created_at=MOMENT,
        updated_at=MOMENT,
    )


def _message(**overrides: Any) -> Message:
    values = {
        "id": MESSAGE_ID,
        "tenant_id": TENANT_ID,
        "conversation_id": CONVERSATION_ID,
        "wa_message_id": "wamid.one",
        "direction": MessageDirection.OUTBOUND,
        "kind": MessageKind.TEXT,
        "status": MessageStatus.SENT,
        "body": "hello",
        "created_at": MOMENT,
        "updated_at": MOMENT,
    }
    values.update(overrides)
    return Message(**values)


class StubInbox:
    """Records the paging arguments the route passed through."""

    def __init__(self) -> None:
        self.conversation_calls: list[dict[str, Any]] = []
        self.message_calls: list[dict[str, Any]] = []
        self.next_cursor: str | None = NEXT_CURSOR

    async def list_conversations(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        priority: ConversationPriority | None = None,
    ) -> Page[Any]:
        self.conversation_calls.append({"limit": limit, "cursor": cursor, "priority": priority})
        return Page(items=[_conversation()], next_cursor=self.next_cursor)

    async def list_messages(
        self, *, conversation_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> Page[Any]:
        self.message_calls.append(
            {"conversation_id": conversation_id, "limit": limit, "cursor": cursor}
        )
        return Page(items=[_message()], next_cursor=self.next_cursor)


class StubMessaging:
    def window_open(self, conversation: Conversation) -> bool:
        return True


class StubSentiment:
    """Records the priority a route asked for, and hands the row back."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_priority(
        self,
        *,
        conversation_id: uuid.UUID,
        priority: ConversationPriority,
    ) -> Conversation:
        self.calls.append({"conversation_id": conversation_id, "priority": priority})
        conversation = _conversation()
        conversation.priority = priority
        return conversation


@pytest.fixture
def inbox(app: FastAPI) -> StubInbox:
    stub = StubInbox()
    app.dependency_overrides[get_inbox_service] = lambda: stub
    app.dependency_overrides[get_messaging_service] = lambda: StubMessaging()
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=TenantRole.TENANT_OWNER,
        ),
        tenant=Tenant(
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )
    return stub


@pytest.fixture
def sentiment(app: FastAPI, inbox: StubInbox) -> StubSentiment:
    stub = StubSentiment()
    app.dependency_overrides[get_sentiment_service] = lambda: stub
    return stub


async def test_the_conversation_list_answers_a_page_not_a_bare_array(
    client: AsyncClient, inbox: StubInbox
) -> None:
    response = await client.get(PATH)

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["items", "next_cursor"]
    assert body["items"][0]["id"] == str(CONVERSATION_ID)
    assert body["next_cursor"] == NEXT_CURSOR


async def test_the_cursor_reaches_the_service(client: AsyncClient, inbox: StubInbox) -> None:
    await client.get(PATH, params={"cursor": NEXT_CURSOR, "limit": 25})

    assert inbox.conversation_calls == [{"limit": 25, "cursor": NEXT_CURSOR, "priority": None}]


async def test_an_exhausted_collection_reports_a_null_cursor(
    client: AsyncClient, inbox: StubInbox
) -> None:
    inbox.next_cursor = None

    body = (await client.get(PATH)).json()

    assert body["next_cursor"] is None


async def test_the_message_list_answers_a_page(client: AsyncClient, inbox: StubInbox) -> None:
    response = await client.get(f"{PATH}/{CONVERSATION_ID}/messages")

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["items", "next_cursor"]
    assert body["items"][0]["id"] == str(MESSAGE_ID)


async def test_the_message_cursor_reaches_the_service(
    client: AsyncClient, inbox: StubInbox
) -> None:
    await client.get(
        f"{PATH}/{CONVERSATION_ID}/messages",
        params={"cursor": NEXT_CURSOR, "limit": 10},
    )

    assert inbox.message_calls[0]["cursor"] == NEXT_CURSOR
    assert inbox.message_calls[0]["limit"] == 10
    assert inbox.message_calls[0]["conversation_id"] == CONVERSATION_ID


async def test_an_over_long_cursor_is_refused_before_it_is_decoded(
    client: AsyncClient, inbox: StubInbox
) -> None:
    response = await client.get(PATH, params={"cursor": "x" * (MAX_CURSOR_LENGTH + 1)})

    assert response.status_code == 422
    assert inbox.conversation_calls == []


async def test_a_limit_beyond_the_bound_is_refused(client: AsyncClient, inbox: StubInbox) -> None:
    assert (await client.get(PATH, params={"limit": 101})).status_code == 422
    assert (await client.get(PATH, params={"limit": 0})).status_code == 422


async def test_a_template_message_reports_its_template_and_no_body(
    client: AsyncClient, inbox: StubInbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    templated = _message(
        kind=MessageKind.TEMPLATE,
        body=None,
        template_name="appointment_reminder",
        template_language="ar_EG",
    )

    async def list_messages(
        *, conversation_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> Page[Any]:
        return Page(items=[templated], next_cursor=None)

    monkeypatch.setattr(inbox, "list_messages", list_messages)

    body = (await client.get(f"{PATH}/{CONVERSATION_ID}/messages")).json()

    message = body["items"][0]
    assert message["kind"] == "template"
    assert message["body"] is None
    assert message["template_name"] == "appointment_reminder"
    assert message["template_language"] == "ar_EG"


async def test_a_text_message_reports_no_template(client: AsyncClient, inbox: StubInbox) -> None:
    body = (await client.get(f"{PATH}/{CONVERSATION_ID}/messages")).json()

    message = body["items"][0]
    assert message["template_name"] is None
    assert message["template_language"] is None


async def test_a_conversation_reports_how_the_customer_sounds(
    client: AsyncClient, inbox: StubInbox
) -> None:
    body = (await client.get(PATH)).json()

    conversation = body["items"][0]
    assert conversation["priority"] == "normal"
    assert conversation["sentiment"] is None
    assert conversation["intent"] is None


async def test_the_priority_filter_reaches_the_service(
    client: AsyncClient, inbox: StubInbox
) -> None:
    await client.get(PATH, params={"priority": "urgent"})

    assert inbox.conversation_calls[0]["priority"] is ConversationPriority.URGENT


async def test_a_priority_that_is_not_one_of_ours_is_refused(
    client: AsyncClient, inbox: StubInbox
) -> None:
    response = await client.get(PATH, params={"priority": "catastrophic"})

    assert response.status_code == 422
    assert inbox.conversation_calls == []


async def test_priority_can_be_set_by_hand(client: AsyncClient, sentiment: StubSentiment) -> None:
    response = await client.post(
        f"{PATH}/{CONVERSATION_ID}/priority",
        json={"priority": "normal"},
    )

    assert response.status_code == 200
    assert response.json()["priority"] == "normal"
    assert sentiment.calls[0]["priority"] is ConversationPriority.NORMAL
    assert sentiment.calls[0]["conversation_id"] == CONVERSATION_ID


async def test_an_unknown_priority_is_refused_before_the_service(
    client: AsyncClient, sentiment: StubSentiment
) -> None:
    response = await client.post(
        f"{PATH}/{CONVERSATION_ID}/priority",
        json={"priority": "on fire"},
    )

    assert response.status_code == 422
    assert sentiment.calls == []
