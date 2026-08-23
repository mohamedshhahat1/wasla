"""Body and duration limits, enforced by the application.

Both are configured in nginx too. These tests exist because nginx is one
deployment topology rather than a property of the software: run the container
directly and every limit configured there is gone, silently, in exactly the
environment where nobody thinks to check.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def limited_settings() -> Settings:
    """Small limits, so a test is a request rather than a wait.

    One second rather than a fraction of one, and the margin is the point: an
    ordinary in-process request here takes single-digit milliseconds, but the
    whole suite running on a loaded machine can stretch that past a tight
    budget - which is a flake caused by the test's stopwatch rather than by the
    middleware. The slow handler below sleeps five seconds, so the timeout still
    fires promptly with a wide margin either side.
    """
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        max_request_bytes=2048,
        request_timeout_seconds=1.0,
    )


@pytest.fixture
def app(limited_settings: Settings, fake_database, fake_redis) -> FastAPI:
    application = create_app(limited_settings)
    application.state.database = fake_database
    application.state.redis = fake_redis

    @application.get("/test/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(5)
        return {"status": "eventually"}

    @application.post("/test/echo")
    async def echo(payload: dict[str, str]) -> dict[str, int]:
        return {"size": len(payload.get("body", ""))}

    return application


@pytest.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http_client:
        yield http_client


# --------------------------------------------------------------- body size


async def test_a_body_under_the_limit_is_accepted(client):
    response = await client.post("/test/echo", json={"body": "x" * 100})

    assert response.status_code == 200
    assert response.json()["size"] == 100


async def test_an_oversized_body_is_refused(client):
    response = await client.post("/test/echo", json={"body": "x" * 4096})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_the_refusal_uses_the_projects_error_envelope(client):
    """Middleware runs before the exception handlers, so the envelope is built
    by hand - a caller should not be able to tell which layer refused them."""
    response = await client.post("/test/echo", json={"body": "x" * 4096})

    body = response.json()
    assert set(body) == {"error"}
    assert "message" in body["error"]


async def test_an_oversized_body_is_refused_before_it_is_read(client, app):
    """The whole point: a limit applied after buffering has already spent the
    memory it exists to protect."""
    seen: list[int] = []

    @app.post("/test/counting")
    async def counting(payload: dict[str, str]) -> dict[str, str]:
        seen.append(len(payload.get("body", "")))
        return {"status": "read"}

    response = await client.post("/test/counting", json={"body": "x" * 8192})

    assert response.status_code == 413
    # The handler never ran, so nothing was buffered on its behalf.
    assert seen == []


async def test_a_body_sent_without_a_declared_length_is_still_capped(client):
    """A request that lies about its length, or sends none at all, is counted
    as it streams."""

    async def oversized():
        for _ in range(8):
            yield b"x" * 512

    response = await client.post("/test/echo", content=oversized())

    # The stream is cut off at the cap; the request cannot succeed.
    assert response.status_code >= 400


# ---------------------------------------------------------------- timeout


async def test_a_handler_that_takes_too_long_is_cut_off(client):
    """A handler waiting on something is holding a pooled database connection
    while it waits, which is the resource being protected."""
    response = await client.get("/test/slow")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_timeout"


async def test_a_normal_request_is_not_affected(client):
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


async def test_the_whatsapp_webhook_is_exempt_from_the_timeout(app):
    """A timed-out delivery is a non-2xx, Meta retries it, and a subscription
    that keeps failing is eventually disabled (ADR-032).

    Asserted against the middleware's own rule rather than by holding a webhook
    open for a second, which would make the test a stopwatch.
    """
    from app.core.limits import RequestTimeoutMiddleware

    middleware = RequestTimeoutMiddleware(app, timeout_seconds=1.0)

    assert middleware._exempt("/api/v1/webhooks/whatsapp") is True
    assert middleware._exempt("/api/v1/conversations") is False


async def test_the_webhook_still_has_a_body_limit(client):
    """A body cap protects memory rather than shedding load, so the reason the
    webhook is exempt from the timeout does not apply to it."""
    response = await client.post(
        "/api/v1/webhooks/whatsapp",
        json={"entry": [{"padding": "x" * 4096}]},
        headers={"X-Hub-Signature-256": "sha256=unverified"},
    )

    assert response.status_code == 413
