"""Two real workspaces, two real sign-ins, one attachment. The hard gate.

Nothing here is stubbed above the database. Both workspaces exist as rows, both
colleagues log in and get their own token, and every request goes through the
whole chain the product uses: token, membership, active workspace, scoped
repository, storage key. The point is to prove tenant isolation on media
end to end rather than by asserting that a repository adds a `WHERE`.

The threat modelled is a colleague at workspace B who **knows** workspace A's
identifiers - the media id, the conversation id and the object key - because
that is the realistic case. Ids travel in URLs, in support tickets and in
screenshots, and a system whose isolation depends on them staying secret does
not have any.

Both backends are exercised: the answer must not depend on whether files are on
a local disk or in a bucket, and the object-store case is the one where a key
prefix could be mistaken for a boundary.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service, get_media_storage
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.object_store import S3MediaStorage
from app.core.security import hash_password
from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.whatsapp import WhatsAppAccount
from app.main import create_app
from tests.conftest import AllowingEntitlements
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import store_object

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
PNG = b"\x89PNG\r\n\x1a\n" + b"tenant-a-private-photograph" * 8


class _Infra:
    """Startup wants a database and a Redis on app state. Neither is used here."""

    async def check(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    @property
    def client(self) -> FakeQueueRedis:
        return FakeQueueRedis()


@pytest.fixture
def isolation_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> MediaStorage:
    if request.param == "local":
        return LocalMediaStorage(tmp_path)
    endpoint = os.environ.get("TEST_S3_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("No object store configured; set TEST_S3_ENDPOINT_URL to run these.")
    return S3MediaStorage(
        bucket=os.environ.get("TEST_S3_BUCKET", "wasla-media"),
        access_key_id=os.environ.get("TEST_S3_ACCESS_KEY_ID", ""),
        secret_access_key=os.environ.get("TEST_S3_SECRET_ACCESS_KEY", ""),
        endpoint_url=endpoint,
        path_style=True,
    )


@pytest.fixture
def app(
    isolation_settings: Settings,
    db_session: AsyncSession,
    storage: MediaStorage,
) -> Iterator[FastAPI]:
    application = create_app(isolation_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    # The one substitution: the store itself, so the same test body runs against
    # a temporary directory and against a real bucket. Everything that decides
    # *who may read* is the real thing.
    application.dependency_overrides[get_media_storage] = lambda: storage
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


async def _workspace(session: AsyncSession, slug: str) -> tuple[Tenant, User]:
    """A workspace with an owner who can actually sign in."""
    suffix = uuid.uuid4().hex[:8]
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{suffix}", status=TenantStatus.ACTIVE)
    user = User(
        email=f"{slug}-{suffix}@example.com",
        full_name=f"{slug.title()} Owner",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    session.add_all([tenant, user])
    await session.flush()
    session.add(
        Membership(
            user_id=user.id,
            tenant_id=tenant.id,
            role=TenantRole.TENANT_OWNER,
            status=TenantStatus.ACTIVE,
        )
    )
    await session.flush()
    return tenant, user


async def _attachment(
    session: AsyncSession,
    tenant: Tenant,
    storage: MediaStorage,
) -> tuple[Conversation, MessageMedia, str]:
    """A conversation with one stored file, exactly as the worker would leave it."""
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
    )
    session.add(conversation)
    await session.flush()

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    key = await store_object(storage, tenant_id=tenant.id, data=PNG, mime_type="image/png")
    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        mime_type="image/png",
        status=MediaStatus.READY,
        storage_key=key,
        storage_state=MediaStorageState.STORED,
        byte_size=len(PNG),
    )
    session.add(media)
    await session.flush()
    return conversation, media, key


async def _sign_in(http: AsyncClient, user: User) -> dict[str, str]:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ============================================================== the hard gate


async def test_the_owner_can_read_their_own_attachment(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The control. Without it, every refusal below could be a broken fixture."""
    tenant, user = await _workspace(db_session, "acme")
    conversation, media, _ = await _attachment(db_session, tenant, storage)

    response = await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}",
        headers=await _sign_in(http, user),
    )

    assert response.status_code == 200
    assert response.content == PNG


async def test_another_workspace_cannot_read_the_attachment(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Workspace B knows both identifiers and is still refused.

    404 rather than 403, following this codebase's convention throughout: a
    distinct status would confirm the id names something real, which is an
    oracle for enumerating another workspace's files.
    """
    tenant_a, _ = await _workspace(db_session, "acme")
    _, user_b = await _workspace(db_session, "globex")
    conversation, media, _ = await _attachment(db_session, tenant_a, storage)

    response = await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}",
        headers=await _sign_in(http, user_b),
    )

    assert response.status_code == 404
    assert PNG not in response.content


async def test_the_bytes_survive_the_refusal(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """A refused read must not be a read that also broke something.

    The owner still gets the file afterwards, which is what separates
    "isolation" from "the file is gone".
    """
    tenant_a, user_a = await _workspace(db_session, "acme")
    _, user_b = await _workspace(db_session, "globex")
    conversation, media, key = await _attachment(db_session, tenant_a, storage)

    await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}",
        headers=await _sign_in(http, user_b),
    )

    assert await storage.get(key) == PNG
    owner = await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}",
        headers=await _sign_in(http, user_a),
    )
    assert owner.status_code == 200


async def test_no_route_lets_another_workspace_delete_the_attachment(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Deletion is not a route at all, and that is the answer to "can B delete it?".

    Asserted rather than assumed: a media DELETE added later without a scoped
    lookup is exactly the mistake this file exists to catch, and this test would
    then start passing for the wrong reason - so it also checks the object is
    still there afterwards.
    """
    tenant_a, _ = await _workspace(db_session, "acme")
    _, user_b = await _workspace(db_session, "globex")
    conversation, media, key = await _attachment(db_session, tenant_a, storage)
    headers = await _sign_in(http, user_b)

    path = f"{API}/conversations/{conversation.id}/media/{media.id}"
    for method in ("delete", "put", "patch", "post"):
        response = await getattr(http, method)(path, headers=headers)
        assert response.status_code in (404, 405), f"{method} answered {response.status_code}"

    assert await storage.get(key) == PNG


async def test_knowing_the_object_key_is_worth_nothing(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The key prefix is a layout, not a boundary.

    A caller cannot supply a key anywhere in this API - the only way to a file is
    a media id that a scoped repository has to find first - and that is the
    property being asserted: `storage_key` appears in no request and no response.
    """
    tenant_a, user_a = await _workspace(db_session, "acme")
    conversation, media, key = await _attachment(db_session, tenant_a, storage)
    headers = await _sign_in(http, user_a)

    listing = await http.get(f"{API}/conversations/{conversation.id}/messages", headers=headers)
    assert listing.status_code == 200
    assert key not in listing.text
    assert "storage_key" not in listing.text

    served = await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}", headers=headers
    )
    assert key not in str(served.headers)


async def test_a_conversation_from_another_workspace_is_not_a_way_in(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Pairing your own conversation id with their media id, and the reverse."""
    tenant_a, _ = await _workspace(db_session, "acme")
    tenant_b, user_b = await _workspace(db_session, "globex")
    conversation_a, media_a, _ = await _attachment(db_session, tenant_a, storage)
    conversation_b, _, _ = await _attachment(db_session, tenant_b, storage)
    headers = await _sign_in(http, user_b)

    mine_theirs = await http.get(
        f"{API}/conversations/{conversation_b.id}/media/{media_a.id}", headers=headers
    )
    assert mine_theirs.status_code == 404

    theirs_theirs = await http.get(
        f"{API}/conversations/{conversation_a.id}/media/{media_a.id}", headers=headers
    )
    assert theirs_theirs.status_code == 404


async def test_an_unauthenticated_caller_gets_nothing(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    tenant, _ = await _workspace(db_session, "acme")
    conversation, media, _ = await _attachment(db_session, tenant, storage)

    response = await http.get(f"{API}/conversations/{conversation.id}/media/{media.id}")

    assert response.status_code == 401
    assert PNG not in response.content


async def test_a_member_of_the_workspace_may_read_it(
    http: AsyncClient, db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Isolation is per workspace, not per person: an inbox is staffed by a team."""
    tenant, _ = await _workspace(db_session, "acme")
    conversation, media, _ = await _attachment(db_session, tenant, storage)

    colleague = User(
        email=f"member-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Inbox Colleague",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db_session.add(colleague)
    await db_session.flush()
    db_session.add(
        Membership(
            user_id=colleague.id,
            tenant_id=tenant.id,
            role=TenantRole.MEMBER,
            status=TenantStatus.ACTIVE,
        )
    )
    await db_session.flush()

    response = await http.get(
        f"{API}/conversations/{conversation.id}/media/{media.id}",
        headers=await _sign_in(http, colleague),
    )

    assert response.status_code == 200
    assert response.content == PNG
