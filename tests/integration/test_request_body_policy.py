"""Expensive unauthenticated bodies are refused before they are parsed (SEC-02).

The audit measured a 9 MiB JSON body to `/auth/login` costing 79 MiB of Python
objects - and costing it again from a client that had already been answered
429, because FastAPI parses a body before it resolves any dependency and the
limiter was one.

Memory is not what these tests measure; a peak is a flaky thing to assert on.
They pin the *architecture* that prevents it: for each refusal, the ASGI
`receive` callable - the only way a body enters the process - is spied on, and
the tests assert it was never called. A refusal that happens after reading the
body cannot pass them, whatever it costs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from app.api.dependencies import get_auth_service
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.limits import BodySizeLimitMiddleware
from app.core.security import create_access_token, create_refresh_token
from app.main import create_app
from tests.conftest import FakeDependency

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
KiB = 1024
MiB = 1024 * KiB


class RefusingAuth:
    """Every login fails, so the status is decided by the limiter or the cap."""

    async def login(self, **kwargs: object) -> None:
        raise AuthenticationError("The credentials are not valid.")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "log_format": "console",
        "log_level": "CRITICAL",
        "cors_origins": [],
        "rate_limit_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _app(settings: Settings, database: FakeDependency, redis: FakeDependency) -> FastAPI:
    app = create_app(settings)
    app.state.database = database
    app.state.redis = redis
    app.dependency_overrides[get_auth_service] = RefusingAuth
    return app


class Spy:
    """An ASGI `receive` that records every call and serves a fixed body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    async def __call__(self) -> Message:
        self.calls += 1
        return {"type": "http.request", "body": self.body, "more_body": False}


async def _call(
    app: Callable[..., Awaitable[None]],
    *,
    path: str,
    declared: int | None,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "POST",
) -> tuple[int, Spy]:
    """One request straight through the ASGI stack. Returns (status, spy)."""
    raw = [(b"content-type", b"application/json"), *(headers or [])]
    if declared is not None:
        raw.append((b"content-length", str(declared).encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": ("203.0.113.9", 50000),
        "server": ("wasla.test", 80),
        "state": {},
    }
    spy = Spy(body)
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, spy, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"]), spy


def _login_body(size: int) -> bytes:
    padding = "x" * max(size - 60, 0)
    return json.dumps({"email": "a@example.com", "password": "pw", "pad": padding}).encode()


# ------------------------------------------------------------ the public cap


@pytest.fixture
def default_app(fake_database: FakeDependency, fake_redis: FakeDependency) -> FastAPI:
    """The shipped caps: nothing here overrides a byte limit."""
    return _app(_settings(), fake_database, fake_redis)


async def test_an_oversized_login_body_is_refused_with_413(default_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=default_app), base_url="http://wasla.test"
    ) as client:
        response = await client.post(LOGIN, content=_login_body(100 * KiB))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_a_known_oversized_body_is_never_read(default_app: FastAPI) -> None:
    """The declared length decides it: `receive` is not called once."""
    status, spy = await _call(default_app, path=LOGIN, declared=9 * MiB)

    assert status == 413
    assert spy.calls == 0


async def test_a_legitimate_login_body_is_still_accepted(default_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=default_app), base_url="http://wasla.test"
    ) as client:
        response = await client.post(LOGIN, json={"email": "a@example.com", "password": "pw"})

    # Reached the service, which refused the credentials - not the size.
    assert response.status_code == 401


async def test_a_body_just_under_the_public_cap_is_parsed(default_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=default_app), base_url="http://wasla.test"
    ) as client:
        response = await client.post(LOGIN, content=_login_body(60 * KiB))

    assert response.status_code != 413


async def test_a_chunked_body_over_the_public_cap_is_refused_with_413(
    default_app: FastAPI,
) -> None:
    """No declared length: counted as it streams, cut off, and answered 413 -
    not whatever the framework makes of a disconnected stream."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"x" * (8 * KiB)

    async with AsyncClient(
        transport=ASGITransport(app=default_app), base_url="http://wasla.test"
    ) as client:
        response = await client.post(LOGIN, content=chunks())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_the_webhook_keeps_its_own_one_mebibyte_cap(default_app: FastAPI) -> None:
    under, _ = await _call(default_app, path="/api/v1/webhooks/whatsapp", declared=None)
    over, spy = await _call(default_app, path="/api/v1/webhooks/whatsapp", declared=MiB + 1)

    assert under != 413
    assert over == 413
    assert spy.calls == 0


# ---------------------------------------------- the limiter runs before the body


@pytest.fixture
def limited_app(fake_database: FakeDependency, fake_redis: FakeDependency) -> FastAPI:
    return _app(
        _settings(rate_limit_enabled=True, rate_limit_auth_per_minute=3),
        fake_database,
        fake_redis,
    )


async def test_a_rate_limited_client_is_refused_before_its_body_is_read(
    limited_app: FastAPI,
) -> None:
    body = _login_body(30 * KiB)
    for _ in range(3):
        status, _ = await _call(limited_app, path=LOGIN, declared=len(body), body=body)
        # Parsed and refused on its content (`extra="forbid"`), and counted.
        assert status == 422

    status, spy = await _call(limited_app, path=LOGIN, declared=len(body), body=body)

    assert status == 429
    assert spy.calls == 0, "the body of a refused request was read"


async def test_one_request_is_counted_once_against_the_login_budget(
    limited_app: FastAPI,
) -> None:
    """The early check and the route dependency are one budget, not two.

    With a budget of three, double counting would refuse the second request.
    """
    statuses = []
    async with AsyncClient(
        transport=ASGITransport(app=limited_app), base_url="http://wasla.test"
    ) as client:
        for _ in range(4):
            response = await client.post(LOGIN, json={"email": "a@example.com", "password": "pw"})
            statuses.append(response.status_code)

    assert statuses == [401, 401, 401, 429]


async def test_the_refusal_still_carries_retry_after(limited_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=limited_app), base_url="http://wasla.test"
    ) as client:
        for _ in range(3):
            await client.post(LOGIN, json={"email": "a@example.com", "password": "pw"})
        response = await client.post(LOGIN, json={"email": "a@example.com", "password": "pw"})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


# ----------------------------------------------------- the authenticated tiers

SECRET = "body-policy-test-signing-key-0123456789-abcdef"


def _tiered() -> tuple[BodySizeLimitMiddleware, list[int], Settings]:
    settings = _settings(jwt_secret=SECRET)
    reached: list[int] = []

    async def downstream(scope: Scope, receive: Any, send: Any) -> None:
        # Reads the whole body, as FastAPI would, so the streaming count runs.
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            if not message.get("more_body"):
                break
        reached.append(1)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = BodySizeLimitMiddleware(
        downstream,
        max_bytes=settings.max_request_bytes,
        webhook_max_bytes=settings.webhook_max_request_bytes,
        json_max_bytes=settings.max_json_request_bytes,
        authenticated_max_bytes=settings.max_authenticated_request_bytes,
        document_max_bytes=settings.max_document_request_bytes,
        settings=settings,
    )
    return middleware, reached, settings


def _bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


def _access(settings: Settings) -> str:
    token, _ = create_access_token(settings=settings, subject=uuid.uuid4())
    return token


MEDIA = "/api/v1/conversations/7d0c9f4e-0000-0000-0000-000000000000/messages/media"
DOCUMENTS = "/api/v1/knowledge/bases/7d0c9f4e-0000-0000-0000-000000000000/documents"
AGENTS = "/api/v1/agents"


@pytest.mark.parametrize(
    ("path", "size", "signed_in", "allowed"),
    [
        (MEDIA, 2 * MiB, False, False),
        (MEDIA, 20 * MiB, True, True),
        (MEDIA, 33 * MiB, True, False),
        (DOCUMENTS, 4 * MiB, True, True),
        (DOCUMENTS, 6 * MiB, True, False),
        (DOCUMENTS, 100 * KiB, False, False),
        (AGENTS, 900 * KiB, True, True),
        (AGENTS, 2 * MiB, True, False),
        (AGENTS, 100 * KiB, False, False),
        (LOGIN, 60 * KiB, False, True),
    ],
)
async def test_each_route_gets_the_cap_its_caller_has_earned(
    path: str, size: int, signed_in: bool, allowed: bool
) -> None:
    middleware, reached, settings = _tiered()
    headers = _bearer(_access(settings)) if signed_in else []

    status, spy = await _call(middleware, path=path, declared=size, headers=headers)

    assert (status != 413) is allowed
    assert bool(reached) is allowed
    if not allowed:
        assert spy.calls == 0


def _forged(settings: Settings) -> str:
    return str(
        jwt.encode(
            {"sub": str(uuid.uuid4()), "typ": "access"},
            "not-the-signing-key-of-this-deployment-0123",
            algorithm="HS256",
        )
    )


def _expired(settings: Settings) -> str:
    expired = settings.model_copy(update={"access_token_ttl_seconds": 1})
    token, _ = create_access_token(settings=expired, subject=uuid.uuid4())
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["exp"] = claims["iat"] - int(timedelta(minutes=5).total_seconds())
    return str(jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm))


def _refresh(settings: Settings) -> str:
    token, _ = create_refresh_token(settings=settings, subject=uuid.uuid4())
    return token


@pytest.mark.parametrize(
    "token_for", [_forged, _expired, _refresh], ids=["forged", "expired", "refresh"]
)
async def test_only_a_verifiable_access_token_earns_a_larger_cap(
    token_for: Callable[[Settings], str],
) -> None:
    middleware, reached, settings = _tiered()

    status, spy = await _call(
        middleware, path=AGENTS, declared=200 * KiB, headers=_bearer(token_for(settings))
    )

    assert status == 413
    assert reached == []
    assert spy.calls == 0


async def test_a_token_does_not_raise_the_webhook_cap() -> None:
    middleware, reached, settings = _tiered()

    status, _ = await _call(
        middleware,
        path="/api/v1/webhooks/whatsapp",
        declared=MiB + 1,
        headers=_bearer(_access(settings)),
    )

    assert status == 413
    assert reached == []


async def test_a_signed_in_chunked_upload_streams_under_its_own_cap() -> None:
    """No declared length and a valid token: counted against the upload cap."""
    middleware, reached, settings = _tiered()

    status, _ = await _call(
        middleware,
        path=MEDIA,
        declared=None,
        body=b"x" * (2 * MiB),
        headers=[*_bearer(_access(settings)), (b"transfer-encoding", b"chunked")],
    )

    assert status == 204
    assert reached == [1]
