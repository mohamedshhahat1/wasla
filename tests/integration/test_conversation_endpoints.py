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
from app.channels.policy import ReplyPolicy
from app.core.pagination import MAX_CURSOR_LENGTH, Cursor, Page
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.channel import (
    Channel,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.sentiment import ConversationPriority
from app.integrations.whatsapp.policy import WhatsAppChannelPolicy

pytestmark = pytest.mark.integration

PATH = "/api/v1/conversations"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONVERSATION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CONTACT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
ACCOUNT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
MESSAGE_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
PARTICIPANT_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
# A synthetic business-scoped id in Meta's documented shape - nobody's.
PARTICIPANT_VALUE = "EG.0synthetic0participant"
MOMENT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
NEXT_CURSOR = Cursor(sort_value=MOMENT, id=CONVERSATION_ID).encode()


def _conversation() -> Conversation:
    return Conversation(
        id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        contact_id=CONTACT_ID,
        account_id=ACCOUNT_ID,
        channel=Channel.WHATSAPP,
        participant_identity_id=PARTICIPANT_ID,
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
        # Set explicitly for the same reason `priority` is above: the
        # column is NOT NULL and this row is never inserted.
        "origin": MessageOrigin.AGENT,
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
        channel: Channel | None = None,
        connection_id: uuid.UUID | None = None,
    ) -> Page[Any]:
        self.conversation_calls.append(
            {
                "limit": limit,
                "cursor": cursor,
                "priority": priority,
                "channel": channel,
                "connection_id": connection_id,
            }
        )
        return Page(items=[_conversation()], next_cursor=self.next_cursor)

    async def participants(
        self, conversations: list[Conversation]
    ) -> dict[uuid.UUID, ContactIdentity]:
        return {
            PARTICIPANT_ID: ContactIdentity(
                id=PARTICIPANT_ID,
                tenant_id=TENANT_ID,
                contact_id=CONTACT_ID,
                channel=Channel.WHATSAPP,
                kind=IdentityKind.BSUID,
                scope=IdentityScope.PROVIDER_ACCOUNT,
                scope_ref="waba-synthetic",
                value=PARTICIPANT_VALUE,
                source=IdentitySource.PROVIDER,
            )
        }

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

    def reply_policy(self, conversation: Conversation) -> ReplyPolicy:
        return WhatsAppChannelPolicy().reply_policy(conversation, now=MOMENT)


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

    assert inbox.conversation_calls == [
        {
            "limit": 25,
            "cursor": NEXT_CURSOR,
            "priority": None,
            "channel": None,
            "connection_id": None,
        }
    ]


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


# ---------------------------------------------- channel-neutral fields (OMNI-015)


async def test_a_conversation_says_which_channel_and_connection_it_is_on(
    client: AsyncClient, inbox: StubInbox
) -> None:
    """Additive: `account_id` keeps its value, and `connection_id` carries the
    same one under the neutral name (docs/API.md)."""
    conversation = (await client.get(PATH)).json()["items"][0]

    assert conversation["channel"] == "whatsapp"
    assert conversation["account_id"] == str(ACCOUNT_ID)
    assert conversation["connection_id"] == str(ACCOUNT_ID)


async def test_a_conversation_names_its_participant_without_the_identifier(
    client: AsyncClient, inbox: StubInbox
) -> None:
    """Who the conversation is with, as its channel addresses them - a kind,
    never the phone number or business-scoped id itself, which an inbox does not
    need and which is personal data."""
    response = await client.get(PATH)
    conversation = response.json()["items"][0]

    assert conversation["participant"] == {
        "id": str(PARTICIPANT_ID),
        "channel": "whatsapp",
        "kind": "bsuid",
    }
    assert PARTICIPANT_VALUE not in response.text


async def test_a_conversation_states_its_reply_policy_beside_the_old_flag(
    client: AsyncClient, inbox: StubInbox
) -> None:
    """`service_window_open` keeps its meaning; `reply_policy` states the rule,
    with the limit in the channel's own unit (ADR-121)."""
    conversation = (await client.get(PATH)).json()["items"][0]

    assert conversation["service_window_open"] is True
    assert conversation["reply_policy"] == {
        "free_text_allowed": True,
        "window_expires_at": "2026-08-22T12:00:00Z",
        "out_of_window": "template",
        "templates": True,
        "text_limit": 4096,
        "text_limit_unit": "characters",
        # Additive (OMNI-031): whether Wasla can act on the channel at all.
        "state": "operational",
        # Additive (OMNI-033): the rule per origin.
        "free_text_mechanism": "standard_window",
        "agent_free_text_allowed": True,
    }


async def test_the_channel_and_connection_filters_reach_the_service(
    client: AsyncClient, inbox: StubInbox
) -> None:
    await client.get(PATH, params={"channel": "whatsapp", "connection_id": str(ACCOUNT_ID)})

    assert inbox.conversation_calls[0]["channel"] is Channel.WHATSAPP
    assert inbox.conversation_calls[0]["connection_id"] == ACCOUNT_ID


async def test_a_channel_that_is_not_one_of_ours_is_refused(
    client: AsyncClient, inbox: StubInbox
) -> None:
    # Telegram and TikTok are vocabulary now (ENT-21); this is not.
    response = await client.get(PATH, params={"channel": "fax"})

    assert response.status_code == 422
    assert inbox.conversation_calls == []


async def test_a_message_carries_its_provider_id_under_the_neutral_name(
    client: AsyncClient, inbox: StubInbox
) -> None:
    message = (await client.get(f"{PATH}/{CONVERSATION_ID}/messages")).json()["items"][0]

    assert message["provider_message_id"] == "wamid.one"
    # Deprecated, not removed: same value until clients have moved.
    assert message["wa_message_id"] == "wamid.one"


async def test_the_deprecated_fields_say_so_in_the_schema(client: AsyncClient) -> None:
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]

    assert schemas["MessageRead"]["properties"]["wa_message_id"]["deprecated"] is True
    assert schemas["ContactOptOutRead"]["properties"]["wa_id"]["deprecated"] is True
    assert "deprecated" not in schemas["ConversationRead"]["properties"]["account_id"]
