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

# How long the timeout tests give a handler, and how long the deliberately slow
# handler takes. A five-fold ratio, so neither assertion is close to its edge.
TIMEOUT_SECONDS = 2.0
SLOW_SECONDS = 10.0


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "log_format": "console",
        "log_level": "CRITICAL",
        "cors_origins": [],
        "rate_limit_enabled": False,
        "max_request_bytes": 2048,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _with_test_routes(application: FastAPI) -> FastAPI:
    @application.get("/test/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(SLOW_SECONDS)
        return {"status": "eventually"}

    @application.post("/test/echo")
    async def echo(payload: dict[str, str]) -> dict[str, int]:
        return {"size": len(payload.get("body", ""))}

    return application


@pytest.fixture
def limited_settings() -> Settings:
    """A small body cap and an ordinary timeout.

    The body cap is what these tests are about, and it is a size comparison
    rather than a stopwatch - so the timeout stays at its default. It used to be
    one second here, which quietly put every body test on a clock: on a loaded
    runner an in-process request that normally takes single-digit milliseconds
    can exceed a one-second budget, and `test_a_body_under_the_limit_is_accepted`
    then fails with 504 for a reason that has nothing to do with body size. That
    is a test failing about its own harness.
    """
    return _settings()


@pytest.fixture
def app(limited_settings: Settings, fake_database, fake_redis) -> FastAPI:
    application = create_app(limited_settings)
    application.state.database = fake_database
    application.state.redis = fake_redis
    return _with_test_routes(application)


@pytest.fixture
async def client(app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http_client:
        yield http_client


@pytest.fixture
def timed_app(fake_database, fake_redis) -> FastAPI:
    """The one application configured to time requests out.

    Only the two tests below need it, and the ratio is what keeps them honest:
    the slow handler sleeps five times the budget, so the refusal is decisive,
    and a request that should *not* be cut off has the whole budget to finish
    in rather than the fraction of a second a tighter one would leave it.
    """
    application = create_app(_settings(request_timeout_seconds=TIMEOUT_SECONDS))
    application.state.database = fake_database
    application.state.redis = fake_redis
    return _with_test_routes(application)


@pytest.fixture
async def timed_client(timed_app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=timed_app),
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


async def test_a_handler_that_takes_too_long_is_cut_off(timed_client):
    """A handler waiting on something is holding a pooled database connection
    while it waits, which is the resource being protected."""
    response = await timed_client.get("/test/slow")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_timeout"


async def test_a_normal_request_is_not_affected(timed_client):
    """The control: the timeout refuses a slow handler, not every handler."""
    response = await timed_client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


async def test_the_whatsapp_webhook_is_exempt_from_the_timeout(app):
    """A timed-out delivery is a non-2xx, Meta retries it, and a subscription
    that keeps failing is eventually disabled (ADR-032).

    Asserted against the middleware's own rule rather than by holding a webhook
    open for a second, which would make the test a stopwatch.
    """
    from app.core.limits import RequestTimeoutMiddleware

    middleware = RequestTimeoutMiddleware(app, timeout_seconds=TIMEOUT_SECONDS)

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
