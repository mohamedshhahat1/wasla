"""The attachment endpoints.

Two things are worth proving at the HTTP layer rather than below it. That a file
is served back with headers which stop a browser executing it - a customer's
upload rendered inline on this origin is a script running against whoever looks
at the inbox. And that the routes are behind the workspace dependency, which is
asserted by calling them with no token at all rather than by reading the code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_media_service,
    get_messaging_service,
)
from app.core.exceptions import NotFoundError
from app.core.storage import StorageError
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.conversation import (
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia

pytestmark = pytest.mark.integration

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
MEDIA_ID = uuid.uuid4()
MESSAGE_ID = uuid.uuid4()

PDF = b"%PDF-1.4 fake"
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _media(**overrides) -> MessageMedia:
    fields = {
        "id": MEDIA_ID,
        "tenant_id": TENANT_ID,
        "message_id": MESSAGE_ID,
        "conversation_id": CONVERSATION_ID,
        "status": MediaStatus.READY,
        "mime_type": "application/pdf",
        "filename": "quote.pdf",
        "byte_size": len(PDF),
        "is_voice": False,
        "attempts": 0,
        **overrides,
    }
    return MessageMedia(**fields)


class StubMediaService:
    """Answers `get` and `read` without a database or a store."""

    def __init__(self, media: MessageMedia | None = None, content: bytes = PDF) -> None:
        self._media = media if media is not None else _media()
        self._content = content
        self.missing = False

    async def get(self, media_id: uuid.UUID) -> MessageMedia:
        if self.missing or media_id != self._media.id:
            # What the scoped repository does for another workspace's row.
            raise NotFoundError()
        return self._media

    async def read(self, media: MessageMedia) -> bytes:
        if self._content is None:
            raise StorageError()
        return self._content


class StubMessaging:
    """Records what the route asked to send."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def window_open(self, conversation) -> bool:
        return True

    async def send_media(self, **kwargs) -> Message:
        self.sent.append(kwargs)
        return Message(
            id=MESSAGE_ID,
            tenant_id=TENANT_ID,
            conversation_id=CONVERSATION_ID,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.DOCUMENT,
            status=MessageStatus.SENT,
            body=kwargs.get("caption"),
            # Filled by the database in real life; supplied here because these
            # rows never reach one.
            created_at=NOW,
            updated_at=NOW,
        )


@pytest.fixture
def media_service(app) -> StubMediaService:
    stub = StubMediaService()
    app.dependency_overrides[get_media_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=TenantRole.TENANT_OWNER,
        ),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )
    return stub


@pytest.fixture
def messaging(app, media_service) -> StubMessaging:
    stub = StubMessaging()
    app.dependency_overrides[get_messaging_service] = lambda: stub
    return stub


async def test_an_attachment_is_served_back(client, media_service):
    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"].startswith("application/pdf")


async def test_an_attachment_is_never_served_inline(client, media_service):
    """A customer-supplied file rendered on this origin is a script.

    The disposition forces a download and `nosniff` stops the browser
    re-deciding the type for itself.
    """
    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_stored_type_outside_the_supported_set_is_not_served_back(client, media_service):
    """A row is input to whoever reads it, whatever wrote it.

    Every row written since SEC-09 was closed holds a type resolved from the
    file's own bytes. A row written by an older release holds whatever the
    caller claimed, and echoing that into a `Content-Type` would keep the
    defect alive for every file already in the store - so the handler serves
    the stored type only if it is one this system canonically supports.
    """
    media_service._media.mime_type = "image/svg+xml"

    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "svg" not in response.headers["content-type"]


async def test_customer_content_is_not_left_in_a_cache(client, media_service):
    """An attachment is one workspace's data served to one authenticated person.

    A shared cache holding it would serve it to the next person through the
    same proxy; a browser cache would leave it on the machine after the session
    that fetched it has gone.
    """
    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    cache_control = response.headers["cache-control"]
    assert "no-store" in cache_control
    assert "public" not in cache_control


async def test_media_from_another_conversation_is_not_found(client, media_service):
    """It exists in this workspace, but not on the conversation named.

    Answered as not-found rather than as a mismatch: a distinct error would
    confirm the id names something real.
    """
    other = uuid.uuid4()
    response = await client.get(f"/api/v1/conversations/{other}/media/{MEDIA_ID}")

    assert response.status_code == 404


async def test_media_from_another_workspace_is_not_found(client, media_service):
    media_service.missing = True

    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.status_code == 404


async def test_a_file_missing_from_the_store_is_not_a_crash(client, media_service):
    media_service._content = None

    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.status_code == 500
    assert "stack" not in response.text.lower()


async def test_an_attachment_can_be_sent(client, messaging):
    response = await client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/media",
        files={"file": ("quote.pdf", PDF, "application/pdf")},
        data={"caption": "here is the quote"},
    )

    assert response.status_code == 201
    assert messaging.sent[0]["content"] == PDF
    assert messaging.sent[0]["mime_type"] == "application/pdf"
    assert messaging.sent[0]["caption"] == "here is the quote"
    # Attributed to the person who sent it, from the token rather than the body.
    assert messaging.sent[0]["sent_by_id"] == USER_ID


async def test_a_caption_is_optional(client, messaging):
    response = await client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/media",
        files={"file": ("photo.jpg", b"jpeg-bytes", "image/jpeg")},
    )

    assert response.status_code == 201
    assert messaging.sent[0]["caption"] is None


async def test_an_empty_upload_is_refused(client, messaging):
    response = await client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/media",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 422
    assert messaging.sent == []


async def test_an_over_long_caption_is_refused(client, messaging):
    response = await client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/media",
        files={"file": ("photo.jpg", b"jpeg-bytes", "image/jpeg")},
        data={"caption": "x" * 2000},
    )

    assert response.status_code == 422
    assert messaging.sent == []


async def test_sending_an_attachment_requires_a_token(client):
    """Takes neither fixture, so nothing is overridden and the real dependency runs.

    Overriding the workspace would bypass the very check being asserted, which
    is how a route can appear tested and be open.
    """
    response = await client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/messages/media",
        files={"file": ("photo.jpg", b"jpeg-bytes", "image/jpeg")},
    )

    assert response.status_code == 401


async def test_downloading_an_attachment_requires_a_token(client):
    response = await client.get(f"/api/v1/conversations/{CONVERSATION_ID}/media/{MEDIA_ID}")

    assert response.status_code == 401
