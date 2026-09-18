"""An inbound file is fetched with the credential of the number it came in on
(MEDIA-13).

Downloads used `settings.meta_access_token` for every workspace, while sends
resolved the number's own encrypted credential (ADR-034). A workspace connected
under its own Meta app could have no attachment downloaded, and a deployment
relying only on workspace credentials none at all.

Through the worker's production client path - no injected WhatsApp factory - so
the token a request carries is the one the worker really chose. Every token is
a sentinel, and every assertion that one is absent is made after proving the
chosen one was present.
"""

from __future__ import annotations

import base64
import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.core.storage import LocalMediaStorage
from app.db.models.media import MediaStatus
from app.services.credential_service import CredentialService
from app.services.media_outcomes import MediaReason, text_for
from app.workers import media_worker as media_worker_module
from app.workers.media_worker import MediaWorker
from tests import media_harness as h

pytestmark = pytest.mark.integration

PLATFORM_TOKEN = "SENTINEL-PLATFORM-TOKEN-a17c"
WORKSPACE_TOKEN = "SENTINEL-WORKSPACE-TOKEN-5e02"
KEY = base64.b64encode(b"k" * 32).decode()
GRAPH = "graph.facebook.com"
CDN = "lookaside.fbsbx.com"


class Meta:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == GRAPH:
            return httpx.Response(
                200,
                json={"url": f"https://{CDN}/x", "mime_type": "image/png", "file_size": 72},
            )
        return httpx.Response(200, content=h.PNG)

    def tokens(self) -> set[str]:
        return {request.headers.get("Authorization", "") for request in self.requests}


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    recorder = Meta()
    monkeypatch.setattr(
        media_worker_module,
        "build_whatsapp_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle)),
    )
    real = socket.getaddrinfo

    def answer(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == CDN:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("157.240.1.36", port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", answer)
    yield recorder


def _production_worker(db_session: AsyncSession, tmp_path: Path, settings: Settings) -> MediaWorker:
    worker = h.worker(
        db_session,
        tmp_path,
        settings,
        storage=LocalMediaStorage(tmp_path),
        reader=h.StubReader(),
    )
    worker._whatsapp_factory = None
    return worker


def _settings(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update=overrides)


def _rendered(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(JsonFormatter().format(record) for record in caplog.records)


async def test_a_number_with_its_own_credential_is_fetched_with_it(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    meta: Meta,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    configured = _settings(
        settings, meta_access_token=PLATFORM_TOKEN, credential_encryption_keys=[KEY]
    )
    where = await h.scene(db_session)
    where.account.access_token_encrypted = CredentialService(configured).seal(
        WORKSPACE_TOKEN, tenant_id=where.tenant.id
    )
    media = await h.attachment(db_session, where)

    job = await h.run(_production_worker(db_session, tmp_path, configured), media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    # Presence first: both hops really were authorised - with the workspace's
    # token, and never the platform's.
    # Descriptor asked by the probe, then by the fetch; then the file.
    assert [request.url.host for request in meta.requests] == [GRAPH, GRAPH, CDN]
    assert meta.tokens() == {f"Bearer {WORKSPACE_TOKEN}"}
    assert job is not None
    # Nowhere else: not on the row, not in any log line.
    row_text = " ".join(str(value) for value in vars(media).values())
    assert WORKSPACE_TOKEN not in row_text and PLATFORM_TOKEN not in row_text
    rendered = _rendered(caplog)
    assert "media.credential_resolved" in rendered
    assert WORKSPACE_TOKEN not in rendered and PLATFORM_TOKEN not in rendered


async def test_a_number_without_its_own_credential_uses_the_platforms(
    db_session: AsyncSession, tmp_path: Path, settings: Settings, meta: Meta
) -> None:
    configured = _settings(settings, meta_access_token=PLATFORM_TOKEN)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)

    await h.run(_production_worker(db_session, tmp_path, configured), media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    assert meta.tokens() == {f"Bearer {PLATFORM_TOKEN}"}


async def test_an_unreadable_workspace_credential_is_never_downgraded_to_the_platforms(
    db_session: AsyncSession, tmp_path: Path, settings: Settings, meta: Meta
) -> None:
    """The workspace asked to be fetched as itself. A key this process cannot
    use ends the file - observably - rather than fetching as somebody else."""
    sealing = _settings(settings, credential_encryption_keys=[KEY])
    where = await h.scene(db_session)
    where.account.access_token_encrypted = CredentialService(sealing).seal(
        WORKSPACE_TOKEN, tenant_id=where.tenant.id
    )
    media = await h.attachment(db_session, where)
    without_key = _settings(
        settings, meta_access_token=PLATFORM_TOKEN, credential_encryption_keys=[]
    )

    job = await h.run(_production_worker(db_session, tmp_path, without_key), media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == text_for(MediaReason.CREDENTIAL_UNAVAILABLE)
    assert meta.requests == []
    assert job is not None


async def test_no_credential_anywhere_fails_the_file_and_releases_the_turn(
    db_session: AsyncSession, tmp_path: Path, settings: Settings, meta: Meta
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)

    job = await h.run(
        _production_worker(db_session, tmp_path, _settings(settings, meta_access_token=None)),
        media,
    )

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == text_for(MediaReason.CREDENTIAL_UNAVAILABLE)
    assert meta.requests == []
    assert job is not None
