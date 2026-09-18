"""Every way a download can fail ends in a terminal row and a released turn
(MEDIA-03, MEDIA-04, MEDIA-10, MEDIA-12).

Driven through the real `MediaWorker` with the real `WhatsAppClient` over a fake
transport, so the HTTP calls counted here are the calls Meta would receive and
the retry budget is the client's own.

The audit's P2-02 is the table this file reverses. A descriptor lookup sat
outside every `try`: a permanent 404 was retried five times by the queue, a 5xx
cost fifteen HTTP calls, and either way the row stayed `pending` for ever and
the conversation's reply never came. Under the locked retry contract
(PD-MEDIA-08) a failure is retried only inside the client, a bounded handful of
times for the transient kinds and not at all for the permanent ones, and then
the file is given up on and the customer is answered.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.media import MediaStatus
from app.integrations.whatsapp.client import WhatsAppClient
from app.services.media_outcomes import MediaReason, text_for
from tests import media_harness as h

pytestmark = pytest.mark.integration

GRAPH = "graph.facebook.com"
CDN = "lookaside.fbsbx.com"


class Meta:
    """A fake Graph API and CDN, counting every request each host receives."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def calls(self, host: str) -> int:
        return sum(1 for request in self.requests if request.url.host == host)

    def client(self) -> WhatsAppClient:
        async def no_sleep(_: float) -> None:
            return None

        return WhatsAppClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(self)),
            access_token="SENTINEL-TERMINAL-STATES",
            sleep=no_sleep,
            jitter=lambda: 0.0,
        )


def _settings(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update=overrides)


# --------------------------------------------------- the descriptor (MEDIA-04)


@pytest.mark.parametrize(
    ("response", "status", "reason", "graph_calls"),
    [
        (
            httpx.Response(404, json={"error": {"code": 100}}),
            MediaStatus.SKIPPED,
            MediaReason.UNAVAILABLE,
            1,
        ),
        (httpx.Response(410), MediaStatus.SKIPPED, MediaReason.UNAVAILABLE, 1),
        (
            httpx.Response(400, json={"error": {"code": 100}}),
            MediaStatus.SKIPPED,
            MediaReason.UNAVAILABLE,
            1,
        ),
        (httpx.Response(403), MediaStatus.FAILED, MediaReason.CREDENTIAL_REFUSED, 1),
        (httpx.Response(401), MediaStatus.FAILED, MediaReason.CREDENTIAL_REFUSED, 1),
        (
            httpx.Response(200, content=b"not json"),
            MediaStatus.FAILED,
            MediaReason.MALFORMED_DESCRIPTOR,
            1,
        ),
        # Transient: the client's own three attempts, and not one more.
        (httpx.Response(429), MediaStatus.FAILED, MediaReason.RATE_LIMITED, 3),
        (httpx.Response(500), MediaStatus.FAILED, MediaReason.DOWNLOAD_FAILED, 3),
        (httpx.Response(503), MediaStatus.FAILED, MediaReason.DOWNLOAD_FAILED, 3),
    ],
)
async def test_a_failed_descriptor_is_terminal_and_releases_the_turn(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    response: httpx.Response,
    status: MediaStatus,
    reason: MediaReason,
    graph_calls: int,
) -> None:
    meta = Meta(lambda request: response)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(db_session, tmp_path, settings, whatsapp=meta.client())

    # Returns - no exception reaches the queue, so the queue retries nothing.
    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is status
    assert media.last_error == text_for(reason)
    assert media.claim_id is None
    assert meta.calls(GRAPH) == graph_calls
    assert meta.calls(CDN) == 0
    assert job is not None
    assert len(h.released(worker)) == 1


async def test_a_transport_failure_on_the_descriptor_is_terminal_after_the_client_budget(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    meta = Meta(refuse)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(db_session, tmp_path, settings, whatsapp=meta.client())

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == text_for(MediaReason.DOWNLOAD_FAILED)
    assert meta.calls(GRAPH) == 3
    assert job is not None


async def test_a_failed_file_is_never_retried_by_a_later_job(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """PD-MEDIA-08, and what MEDIA-12 found the documentation promising
    instead: a second job for the same file asks Meta nothing."""
    meta = Meta(lambda request: httpx.Response(503))
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)

    await h.run(h.worker(db_session, tmp_path, settings, whatsapp=meta.client()), media)
    asked = len(meta.requests)
    await h.run(h.worker(db_session, tmp_path, settings, whatsapp=meta.client()), media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.attempts == 1
    assert len(meta.requests) == asked


# ------------------------------------------------ decisions made before a byte


async def test_a_file_declared_over_the_cap_is_never_fetched(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    whatsapp = h.StubWhatsApp(content=h.PNG * 10)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(
        db_session, tmp_path, _settings(settings, media_max_bytes=64), whatsapp=whatsapp
    )

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == text_for(MediaReason.OVERSIZE)
    assert whatsapp.fetches == 0
    assert job is not None


async def test_an_inbound_video_is_skipped_before_it_is_downloaded(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """PD-MEDIA-04 as it is, not as MEDIA.md said: a video is neither
    downloaded nor stored."""
    whatsapp = h.StubWhatsApp(content=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4")
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where, mime_type="video/mp4")
    worker = h.worker(db_session, tmp_path, settings, whatsapp=whatsapp)

    await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == text_for(MediaReason.UNSUPPORTED_TYPE)
    assert whatsapp.fetches == 0
    assert media.storage_key is None
    assert list(tmp_path.rglob("*")) == []


async def test_an_enormous_provider_type_string_cannot_overflow_the_reason(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The column-overflow half of MEDIA-04. A descriptor type of 600
    characters used to be interpolated into `last_error` (500), failing the
    write and stranding the turn. No provider text reaches the reason now."""
    long_type = "application/x-" + "a" * 600
    whatsapp = h.StubWhatsApp(mime_type=long_type)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where, mime_type=None)
    worker = h.worker(db_session, tmp_path, settings, whatsapp=whatsapp)

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == text_for(MediaReason.UNSUPPORTED_TYPE)
    assert "aaaa" not in (media.last_error or "")
    assert job is not None


async def test_a_worker_with_no_credential_fails_the_file_rather_than_holding_it(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """P2-06, reversed. With no token, five rounds used to end in a dead
    letter and a row `pending` for ever."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(db_session, tmp_path, _settings(settings, meta_access_token=None))
    worker._whatsapp_factory = None

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == text_for(MediaReason.CREDENTIAL_UNAVAILABLE)
    assert job is not None


# ------------------------------------------------------ the deadline (MEDIA-10)


class Drip(httpx.AsyncByteStream):
    """A body that never ends: one byte every 50 ms, for ever."""

    def __init__(self) -> None:
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.05)
            self.sent += 1
            yield b"\xff" if self.sent == 1 else b"d"

    async def aclose(self) -> None:
        self.closed = True


async def test_a_body_that_drips_for_ever_hits_the_total_deadline(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-07/P1-08, reversed. Every read completes inside any per-read
    timeout, so only a wall clock over the whole download can stop it."""
    import socket

    real = socket.getaddrinfo

    def answer(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == CDN:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("157.240.1.36", port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", answer)
    drip = Drip()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return httpx.Response(
                200, json={"url": f"https://{CDN}/x", "mime_type": "image/png", "file_size": 64}
            )
        return httpx.Response(200, stream=drip)

    meta = Meta(handler)
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    deadline = 1.0
    worker = h.worker(
        db_session,
        tmp_path,
        _settings(settings, media_download_deadline_seconds=deadline),
        whatsapp=meta.client(),
    )

    started = time.perf_counter()
    # Bounded here too, so a missing deadline fails this test instead of
    # hanging it: the drip really does never end.
    job = await asyncio.wait_for(h.run(worker, media), timeout=deadline + 10)
    elapsed = time.perf_counter() - started

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == text_for(MediaReason.TIMEOUT)
    assert deadline <= elapsed < deadline + 3
    # It really was dripping, and it was stopped - not left to run on.
    assert drip.sent >= 5
    sent_at_stop = drip.sent
    await asyncio.sleep(0.3)
    assert drip.sent == sent_at_stop
    # Nothing was written, and the turn was released.
    assert media.storage_key is None
    assert list(tmp_path.rglob("*")) == []
    assert job is not None


def test_a_download_deadline_the_queue_lease_cannot_cover_is_refused() -> None:
    with pytest.raises(ValueError, match="media_download_deadline_seconds"):
        Settings(
            _env_file=None,
            environment="test",
            queue_visibility_timeout_seconds=120.0,
            media_download_deadline_seconds=120.0,
        )


def test_the_default_download_deadline_is_inside_the_default_lease() -> None:
    defaults = Settings(_env_file=None, environment="test")
    assert defaults.media_download_deadline_seconds < defaults.queue_visibility_timeout_seconds
