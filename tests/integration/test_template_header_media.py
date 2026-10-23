"""Header media for a person's and a follow-up's template, from the workspace library (HMT).

Through the real ASGI application with real tokens and the real follow-up
worker; Meta at the transport counts every upload and every send. A manual
IMAGE-header template names a library file: Wasla checks it, uploads it once
per (file, number) and reuses the id while fresh, and builds the header. Raw
media parameters from a client are refused; another workspace's file is not
found; deleting a file a follow-up waits on is refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api.dependencies import get_media_storage
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models.conversation import Conversation
from app.db.models.enums import TenantRole
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.whatsapp_template import WhatsAppTemplate
from app.db.session import Database
from app.main import create_app
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from app.workers.follow_up_worker import FollowUpWorker
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import (
    PLATFORM_TOKEN,
    FakeMeta,
    Workspace,
    erase,
    harness_settings,
    seed,
    store_file,
    worker,
)
from tests.integration.test_manual_marketing_templates import _person

pytestmark = pytest.mark.integration

IMAGE_HEADER: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aGFuZGxl"]}},
    {"type": "BODY", "text": "Your order is ready."},
]


@pytest_asyncio.fixture
async def inbox(
    prepared_database: str,
    settings: Settings,
    fake_redis: FakeDependency,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage]]:
    import app.services.messaging_service as messaging_module

    meta = FakeMeta()
    storage = LocalMediaStorage(tmp_path)
    _, database = worker(prepared_database, meta, name="hmt")
    monkeypatch.setattr(messaging_module, "build_http_client", lambda: meta.client("manual"))
    configured = settings.model_copy(update={"meta_access_token": PLATFORM_TOKEN})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    application.dependency_overrides[get_media_storage] = lambda: storage
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield meta, database, client, configured, storage
    finally:
        await erase(database)
        await database.dispose()


async def _first(database: Database, workspace: Workspace) -> Conversation:
    async with database.session_factory() as session:
        found = await session.scalar(
            select(Conversation)
            .where(Conversation.tenant_id == workspace.tenant_id)
            .order_by(Conversation.id)
        )
        assert found is not None
        return found


async def _template(database: Database, workspace: Workspace) -> WhatsAppTemplate:
    async with database.session_factory() as session:
        row = await session.get(WhatsAppTemplate, workspace.template_id)
        assert row is not None
        return row


def _image_ids(meta: FakeMeta) -> list[str | None]:
    return meta.header_ids()


async def test_a_manual_image_template_uploads_once_and_reuses_the_id(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
) -> None:
    meta, database, client, settings, storage = inbox
    workspace = await seed(database, recipients=2, components=IMAGE_HEADER)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    asset_id = await store_file(database, storage, workspace)
    template = await _template(database, workspace)
    async with database.session_factory() as session:
        conversations = list(
            (
                await session.execute(
                    select(Conversation).where(Conversation.tenant_id == workspace.tenant_id)
                )
            ).scalars()
        )

    for conversation in conversations:
        answer = await client.post(
            f"/api/v1/conversations/{conversation.id}/messages/template",
            json={
                "name": template.name,
                "language": template.language,
                "header_media_id": str(asset_id),
            },
            headers=admin,
        )
        assert answer.status_code == 201, answer.text

    assert len(meta.uploads) == 1, "one upload per file and number while the id is fresh"
    ids = _image_ids(meta)
    assert len(ids) == 2 and ids[0] is not None and ids[0] == ids[1]
    assert str(ids[0]) not in answer.text, "Meta's id never leaves the server"


@pytest.mark.parametrize(
    ("body", "code", "status"),
    [
        (
            {
                "components": [
                    {"type": "header", "parameters": [{"type": "image", "image": {"id": "123"}}]}
                ]
            },
            "header_media_must_be_uploaded",
            422,
        ),
        (
            {
                "components": [
                    {
                        "type": "header",
                        "parameters": [
                            {"type": "image", "image": {"link": "https://x.test/a.png"}}
                        ],
                    }
                ]
            },
            "header_media_must_be_uploaded",
            422,
        ),
        ({}, "header_media_required", 422),
    ],
)
async def test_raw_or_missing_header_media_is_refused(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
    body: dict[str, Any],
    code: str,
    status: int,
) -> None:
    meta, database, client, settings, _ = inbox
    workspace = await seed(database, recipients=1, components=IMAGE_HEADER)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    template = await _template(database, workspace)
    conversation = await _first(database, workspace)
    answer = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages/template",
        json={"name": template.name, "language": template.language, **body},
        headers=admin,
    )
    assert answer.status_code == status, answer.text
    assert answer.json()["error"]["code"] == code
    assert meta.requests == [] and meta.uploads == []


async def test_a_wrong_kind_and_another_workspace_s_file_are_refused(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
) -> None:
    meta, database, client, settings, storage = inbox
    workspace = await seed(database, recipients=1, components=IMAGE_HEADER)
    other = await seed(database, recipients=1, components=IMAGE_HEADER)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    template = await _template(database, workspace)
    conversation = await _first(database, workspace)
    pdf = await store_file(
        database,
        storage,
        workspace,
        content=b"%PDF-1.4 a price list" * 20,
        kind="document",
        mime_type="application/pdf",
        filename="prices.pdf",
    )
    theirs = await store_file(database, storage, other)
    url = f"/api/v1/conversations/{conversation.id}/messages/template"
    base = {"name": template.name, "language": template.language}

    mismatch = await client.post(url, json={**base, "header_media_id": str(pdf)}, headers=admin)
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "header_media_kind_mismatch"
    foreign = await client.post(url, json={**base, "header_media_id": str(theirs)}, headers=admin)
    assert foreign.status_code == 404
    assert meta.requests == [] and meta.uploads == []


async def test_the_library_lists_uploads_and_refuses_a_member(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
) -> None:
    _, database, client, settings, storage = inbox
    workspace = await seed(database, recipients=1)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    member = await _person(database, settings, workspace, TenantRole.MEMBER)
    png = b"\x89PNG\r\n\x1a\n" + b"a kitchen photograph" * 40

    refused = await client.post(
        "/api/v1/template-header-media",
        files={"file": ("photo.png", png, "image/png")},
        headers=member,
    )
    assert refused.status_code == 403
    uploaded = await client.post(
        "/api/v1/template-header-media",
        files={"file": ("photo.png", png, "image/png")},
        headers=admin,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["kind"] == "image"
    listed = await client.get("/api/v1/template-header-media", headers=member)
    assert [item["id"] for item in listed.json()["items"]] == [uploaded.json()["id"]]
    assert (await client.get("/api/v1/template-header-media?kind=video", headers=member)).json()[
        "items"
    ] == []
    gif = await client.post(
        "/api/v1/template-header-media",
        files={"file": ("a.gif", b"GIF89a" + b"\x00" * 64, "image/gif")},
        headers=admin,
    )
    assert gif.status_code == 422

    deleted = await client.delete(
        f"/api/v1/template-header-media/{uploaded.json()['id']}", headers=admin
    )
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/template-header-media", headers=member)).json()["items"] == []


async def test_a_follow_up_template_sends_its_file_and_the_file_cannot_be_deleted_meanwhile(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
    prepared_database: str,
) -> None:
    meta, database, client, settings, storage = inbox
    workspace = await seed(database, recipients=1, components=IMAGE_HEADER)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    asset_id = await store_file(database, storage, workspace)
    template = await _template(database, workspace)
    conversation = await _first(database, workspace)
    async with database.session_factory() as session:
        follow_up = await FollowUpService(session=session, tenant_id=workspace.tenant_id).schedule(
            conversation_id=conversation.id,
            delay=timedelta(minutes=5),
            template_name=template.name,
            template_language=template.language,
            header_media_id=asset_id,
            created_by_kind=ActorKind.USER,
        )
        await session.commit()
        follow_up_id = follow_up.id

    in_use = await client.delete(f"/api/v1/template-header-media/{asset_id}", headers=admin)
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "header_media_in_use"

    async with database.session_factory() as session:
        await session.execute(
            update(FollowUp)
            .where(FollowUp.id == follow_up_id)
            .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    worker_settings = harness_settings(prepared_database)
    http = meta.client("follow-ups")
    sweeper = FollowUpWorker(
        database=database,
        settings=worker_settings,
        messaging_factory=lambda session, tenant: MessagingService(
            session=session, settings=worker_settings, tenant_id=tenant, http=http
        ),
        storage=storage,
    )
    await sweeper.run_once()

    async with database.session_factory() as session:
        sent = await session.get(FollowUp, follow_up_id)
        assert sent is not None and sent.status is FollowUpStatus.SENT, sent and sent.last_error
    assert len(meta.uploads) == 1
    (header_id,) = _image_ids(meta)
    assert header_id is not None
    assert (
        await client.delete(f"/api/v1/template-header-media/{asset_id}", headers=admin)
    ).status_code == 204


async def test_a_follow_up_template_without_its_file_is_refused_at_scheduling(
    inbox: tuple[FakeMeta, Database, AsyncClient, Settings, MediaStorage],
) -> None:
    _, database, _, _, _ = inbox
    workspace = await seed(database, recipients=1, components=IMAGE_HEADER)
    template = await _template(database, workspace)
    conversation = await _first(database, workspace)
    from app.core.exceptions import ValidationError

    async with database.session_factory() as session:
        with pytest.raises(ValidationError) as refused:
            await FollowUpService(session=session, tenant_id=workspace.tenant_id).schedule(
                conversation_id=conversation.id,
                delay=timedelta(minutes=5),
                template_name=template.name,
                template_language=template.language,
                created_by_kind=ActorKind.USER,
            )
    assert refused.value.error_code == "header_media_required"
