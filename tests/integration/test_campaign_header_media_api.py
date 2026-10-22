"""A campaign header file: uploaded, typed by its bytes, composed, previewed (HM-01..HM-08).

Through the real application (`create_app`), the real routes, the real
workspace authorization and the real write protocol, against a local
directory and a real S3-compatible bucket. Nothing here reaches Meta: the
upload to Meta is the campaign worker's (`test_campaign_header_media.py`).

Meta's limits are its own ("Media", developers.facebook.com/documentation/
business-messaging/whatsapp/business-phone-numbers/media.md, read 2026-10-15):
JPEG and PNG images up to 5 MB, MP4 and 3GPP video up to 16 MB.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service, get_media_storage
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.object_store import S3MediaStorage
from app.core.security import create_access_token
from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.media import MediaStorageState
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.main import create_app
from tests.channel_plans import allow_channels
from tests.conftest import AllowingEntitlements
from tests.fake_queue_redis import FakeQueueRedis
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"a spring sale photograph" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"a spring sale photograph" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
GIF = b"GIF89a" + b"\x00" * 64
MB = 1024 * 1024


def _header(format_: str) -> list[dict[str, Any]]:
    return [
        {"type": "HEADER", "format": format_, "example": {"header_handle": ["4::aGFuZGxl"]}},
        {"type": "BODY", "text": "Spring sale."},
    ]


class _Infra:
    async def check(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    @property
    def client(self) -> FakeQueueRedis:
        return FakeQueueRedis()


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
def app(settings: Settings, db_session: AsyncSession, storage: MediaStorage) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    application.dependency_overrides[get_media_storage] = lambda: storage
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


class Desk:
    """One workspace: an owner, a member, a number and one template per header format."""

    def __init__(self) -> None:
        self.tenant_id = uuid.uuid4()
        self.account_id = uuid.uuid4()
        self.admin: dict[str, str] = {}
        self.member: dict[str, str] = {}
        self.templates: dict[str, uuid.UUID] = {}


async def _desk(
    session: AsyncSession, settings: Settings, *, limits: dict[str, int] | None = None
) -> Desk:
    desk = Desk()
    tag = uuid.uuid4().hex[:8]
    tenant = Tenant(
        timezone=TEST_TIMEZONE,
        id=desk.tenant_id,
        name=f"Media {tag}",
        slug=f"hm-{tag}",
        status=TenantStatus.ACTIVE,
    )
    owner = User(
        email=f"owner@{tag}.example", hashed_password="x", email_verified_at=datetime.now(UTC)
    )
    member = User(
        email=f"member@{tag}.example", hashed_password="x", email_verified_at=datetime.now(UTC)
    )
    session.add_all([tenant, owner, member])
    await session.flush()
    await allow_channels(session, tenant.id, limits=limits)
    session.add_all(
        [
            Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER),
            Membership(tenant_id=tenant.id, user_id=member.id, role=TenantRole.MEMBER),
            WhatsAppAccount(
                id=desk.account_id,
                tenant_id=tenant.id,
                phone_number_id=f"pn-{tag}",
                waba_id=f"waba-{tag}",
                display_phone_number="+201000000000",
            ),
        ]
    )
    await session.flush()
    for format_ in ("IMAGE", "VIDEO", "DOCUMENT", "LOCATION", "GIF", "TEXT"):
        components = (
            [{"type": "HEADER", "format": "TEXT", "text": "Sale"}, {"type": "BODY", "text": "Hi"}]
            if format_ == "TEXT"
            else _header(format_)
        )
        template = WhatsAppTemplate(
            tenant_id=tenant.id,
            account_id=desk.account_id,
            name=f"sale_{format_.lower()}_{tag}",
            language="ar_EG",
            category=TemplateCategory.MARKETING,
            status=TemplateStatus.APPROVED,
            body_text="Spring sale.",
            variable_count=0,
            components=components,
        )
        session.add(template)
        await session.flush()
        desk.templates[format_] = template.id
    for user, headers in ((owner, desk.admin), (member, desk.member)):
        token, _ = create_access_token(
            settings=settings,
            subject=user.id,
            tenant_id=tenant.id,
            token_version=user.token_version,
        )
        headers["Authorization"] = f"Bearer {token}"
    return desk


async def _upload(
    http: AsyncClient,
    desk: Desk,
    content: bytes,
    *,
    name: str = "spring.png",
    content_type: str = "image/png",
    headers: dict[str, str] | None = None,
) -> Any:
    return await http.post(
        "/api/v1/campaigns/header-media",
        headers=headers or desk.admin,
        files={"file": (name, content, content_type)},
    )


async def _compose(http: AsyncClient, desk: Desk, format_: str, header_media_id: str | None) -> Any:
    body: dict[str, Any] = {
        "account_id": str(desk.account_id),
        "template_id": str(desk.templates[format_]),
        "name": f"Spring {format_}",
    }
    if header_media_id is not None:
        body["header_media_id"] = header_media_id
    return await http.post("/api/v1/campaigns", headers=desk.admin, json=body)


# ---------------------------------------------------------------- the upload


@pytest.mark.parametrize(
    ("content", "name", "content_type", "kind", "mime_type"),
    [
        (PNG, "spring.png", "image/png", "image", "image/png"),
        (JPEG, "spring.jpg", "image/jpeg", "image", "image/jpeg"),
        (MP4, "spring.mp4", "video/mp4", "video", "video/mp4"),
        (PDF, "Spring catalogue.pdf", "application/pdf", "document", "application/pdf"),
    ],
)
async def test_an_upload_is_stored_with_its_kind_size_and_hash(
    http: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    storage: MediaStorage,
    content: bytes,
    name: str,
    content_type: str,
    kind: str,
    mime_type: str,
) -> None:
    desk = await _desk(db_session, settings)

    response = await _upload(http, desk, content, name=name, content_type=content_type)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == kind
    assert body["mime_type"] == mime_type
    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert body["filename"] == (name if kind == "document" else None)
    row = await db_session.get(CampaignHeaderMedia, uuid.UUID(body["id"]))
    assert row is not None and row.tenant_id == desk.tenant_id
    assert row.storage_state is MediaStorageState.STORED
    assert row.storage_key is not None
    assert await storage.get(row.storage_key) == content
    # The response names no key and no link: the bytes come back through the API.
    assert row.storage_key not in response.text and "http" not in json.dumps(body)
    audited = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.target_id == row.id,
            AuditLog.action == AuditAction.CAMPAIGN_HEADER_MEDIA_UPLOADED,
        )
    )
    assert audited is not None and set(audited.meta or {}) == {"kind", "byte_size"}


async def test_the_kind_is_the_bytes_never_the_name_or_the_claim(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)

    renamed = await _upload(http, desk, PNG, name="catalogue.pdf", content_type="image/png")
    assert renamed.status_code == 201 and renamed.json()["kind"] == "image"

    unclaimed = await _upload(
        http, desk, PNG, name="photo.bin", content_type="application/octet-stream"
    )
    assert unclaimed.status_code == 201, unclaimed.text
    assert unclaimed.json()["kind"] == "image"

    pdf_claimed_png = await _upload(http, desk, PDF, name="photo.png", content_type="image/png")
    assert pdf_claimed_png.status_code == 422

    gif = await _upload(http, desk, GIF, name="spring.gif", content_type="image/gif")
    assert gif.status_code == 422
    assert gif.json()["error"]["code"] == "header_media_type"

    stored = await db_session.scalar(
        select(func.count())
        .select_from(CampaignHeaderMedia)
        .where(CampaignHeaderMedia.tenant_id == desk.tenant_id)
    )
    assert stored == 2


async def test_over_metas_image_limit_is_refused_naming_the_limit(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)

    response = await _upload(http, desk, PNG + b"\x00" * (5 * MB))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "header_media_too_large"
    assert "5 MB" in response.json()["error"]["message"]


async def test_over_the_upload_cap_is_refused_within_a_chunk(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)

    response = await _upload(
        http, desk, MP4 + b"\x00" * (16 * MB), name="long.mp4", content_type="video/mp4"
    )

    assert response.status_code == 422
    assert "too large" in response.json()["error"]["message"]


async def test_over_the_storage_allowance_is_402_and_nothing_is_left_behind(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings, limits={"storage_bytes": 100})

    response = await _upload(http, desk, PNG)

    assert response.status_code == 402
    rows = await db_session.scalar(
        select(func.count())
        .select_from(CampaignHeaderMedia)
        .where(CampaignHeaderMedia.tenant_id == desk.tenant_id)
    )
    assert rows == 0


async def test_a_member_cannot_upload_but_can_read(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)

    refused = await _upload(http, desk, PNG, headers=desk.member)
    assert refused.status_code == 403

    stored = await _upload(http, desk, PNG)
    asset_id = stored.json()["id"]
    read = await http.get(f"/api/v1/campaigns/header-media/{asset_id}", headers=desk.member)
    assert read.status_code == 200 and read.json()["kind"] == "image"
    assert "sha256" not in read.json()

    file = await http.get(f"/api/v1/campaigns/header-media/{asset_id}/file", headers=desk.member)
    assert file.status_code == 200
    assert file.content == PNG
    assert file.headers["content-type"] == "image/png"
    assert file.headers["content-disposition"] == "attachment"
    assert file.headers["x-content-type-options"] == "nosniff"


async def test_another_workspaces_file_is_not_found_anywhere(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    ours = await _desk(db_session, settings)
    theirs = await _desk(db_session, settings)
    their_file = (await _upload(http, theirs, PNG)).json()["id"]

    for path in (
        f"/api/v1/campaigns/header-media/{their_file}",
        f"/api/v1/campaigns/header-media/{their_file}/file",
    ):
        assert (await http.get(path, headers=ours.admin)).status_code == 404

    composed = await _compose(http, ours, "IMAGE", their_file)
    assert composed.status_code == 404


# ---------------------------------------------------------------- composition


async def test_a_media_header_template_composes_with_a_file_of_its_kind(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)
    image = (await _upload(http, desk, PNG)).json()
    video = (await _upload(http, desk, MP4, name="v.mp4", content_type="video/mp4")).json()
    document = (
        await _upload(http, desk, PDF, name="Catalogue.pdf", content_type="application/pdf")
    ).json()

    for format_, asset in (("IMAGE", image), ("VIDEO", video), ("DOCUMENT", document)):
        response = await _compose(http, desk, format_, asset["id"])
        assert response.status_code == 201, response.text
        preview = response.json()["header_media"]
        assert preview == {
            "id": asset["id"],
            "kind": asset["kind"],
            "mime_type": asset["mime_type"],
            "size_bytes": asset["size_bytes"],
            "filename": asset["filename"],
            "created_at": asset["created_at"],
        }
        campaign_id = response.json()["id"]
        read = await http.get(f"/api/v1/campaigns/{campaign_id}", headers=desk.member)
        assert read.json()["header_media"]["id"] == asset["id"]

    listed = await http.get("/api/v1/campaigns", headers=desk.admin)
    assert {item["header_media"]["kind"] for item in listed.json()["items"]} == {
        "image",
        "video",
        "document",
    }


@pytest.mark.parametrize(
    ("format_", "upload", "code"),
    [
        ("IMAGE", "pdf", "template_parameters"),
        ("VIDEO", "png", "template_parameters"),
        ("DOCUMENT", "mp4", "template_parameters"),
        ("IMAGE", None, "template_parameters"),
        ("TEXT", "png", "template_parameters"),
        ("LOCATION", None, "template_unsupported"),
        ("GIF", None, "template_unsupported"),
        ("GIF", "png", "template_unsupported"),
    ],
)
async def test_a_wrong_missing_or_extra_file_is_refused_at_composition(
    http: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    format_: str,
    upload: str | None,
    code: str,
) -> None:
    desk = await _desk(db_session, settings)
    files = {
        "png": (PNG, "a.png", "image/png"),
        "pdf": (PDF, "a.pdf", "application/pdf"),
        "mp4": (MP4, "a.mp4", "video/mp4"),
    }
    asset_id = None
    if upload is not None:
        content, name, content_type = files[upload]
        uploaded = await _upload(http, desk, content, name=name, content_type=content_type)
        asset_id = uploaded.json()["id"]

    response = await _compose(http, desk, format_, asset_id)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == code
    if code == "template_parameters":
        assert "header" in error["message"]


async def test_no_campaign_response_carries_metas_id_or_a_key(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    desk = await _desk(db_session, settings)
    asset = (await _upload(http, desk, PNG)).json()
    campaign = (await _compose(http, desk, "IMAGE", asset["id"])).json()

    assert "header_provider_media_id" not in campaign
    assert "header_media_id" not in campaign
    row = await db_session.get(CampaignHeaderMedia, uuid.UUID(asset["id"]))
    assert row is not None and row.storage_key is not None
    assert row.storage_key not in json.dumps(campaign)


async def test_the_header_file_changes_only_through_a_draft_s_versioned_edit(
    http: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """A draft's file changes through the draft edit, from the version read (HM-02, PG-03).

    Before this stage `PATCH` answered 405; it now edits drafts only, and an
    edit that does not say which version it was made from is refused. The
    audience and schedule requests still refuse an unknown field rather than
    ignoring it.
    """
    desk = await _desk(db_session, settings)
    first = (await _upload(http, desk, PNG)).json()["id"]
    second = (await _upload(http, desk, JPEG, name="b.jpg", content_type="image/jpeg")).json()["id"]
    composed = (await _compose(http, desk, "IMAGE", first)).json()
    campaign = composed["id"]

    unversioned = await http.patch(
        f"/api/v1/campaigns/{campaign}", headers=desk.admin, json={"header_media_id": second}
    )
    assert unversioned.status_code == 422
    assert unversioned.json()["error"]["code"] == "campaign_version_required"
    for step in ("audience", "schedule"):
        path = f"/api/v1/campaigns/{campaign}/{step}"
        refused = await http.post(path, headers=desk.admin, json={"header_media_id": second})
        assert refused.status_code == 422
    read = await http.get(f"/api/v1/campaigns/{campaign}", headers=desk.admin)
    assert read.json()["header_media"]["id"] == first

    edited = await http.patch(
        f"/api/v1/campaigns/{campaign}",
        headers={**desk.admin, "If-Match": f'"{composed["version"]}"'},
        json={"header_media_id": second},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["header_media"]["id"] == second
    assert edited.json()["version"] == composed["version"] + 1
