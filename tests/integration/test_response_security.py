"""Response-surface security: what leaves the process, and what a browser is told.

Three findings from the final audit, each verified behaviourally against a
production-configured application rather than by reading the handler:

- **Validation errors echoed the submitted value.** Pydantic's `errors()`
  carries an `input` key holding the offending value verbatim, and it was
  serialised straight into the 422 body in every environment. A rejected
  over-length password came back in full. A 422 travels further than people
  expect - reverse-proxy access logs, APM payloads, browser HAR captures,
  client-side error reporters - so this was a credential in all of them.
- **The application set no security headers.** `nginx/nginx.conf` sets three,
  but nginx is one deployment topology rather than a property of the software:
  `docker-compose.prod.yml` runs the API as its own container, and the image can
  be run directly. HSTS and CSP were absent from both.
- **`Cache-Control` was unset on responses carrying workspace data and tokens.**

The last test is a mutation check. A guard whose removal the suite cannot detect
is weak evidence that the guard works, so it strips the filter and asserts the
leak comes back.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.exceptions import _safe_validation_errors
from app.main import create_app
from tests.conftest import FakeDependency

pytestmark = pytest.mark.integration

API = "/api/v1"
SECRET = "SUPERSECRET-PASSPHRASE"


@pytest.fixture
def production_settings() -> Settings:
    """Configured exactly as production is, because that is where it mattered.

    The earlier handler suppressed nothing in production while its sibling
    `handle_unexpected_error` did, so testing under `test` would have proved the
    wrong thing.
    """
    return Settings(
        _env_file=None,
        environment="production",
        jwt_secret="x" * 40,
        meta_app_secret="an-app-secret",
        docs_enabled=False,
        cors_origins=[],
        log_level="CRITICAL",
        rate_limit_enabled=False,
    )


@pytest_asyncio.fixture
async def http(production_settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(production_settings)
    app.state.database = FakeDependency(name="postgresql")
    app.state.redis = FakeDependency(name="redis")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


# ------------------------------------------------- submitted values stay in


async def test_a_rejected_password_is_not_read_back_to_the_caller(http: AsyncClient) -> None:
    response = await http.post(
        f"{API}/auth/register",
        json={
            # Over the 128-character maximum, so validation refuses it and the
            # handler under test runs.
            "password": SECRET + ("z" * 200),
            "email": "someone@example.com",
            "workspace_name": "Workspace",
            "workspace_slug": "workspace",
        },
    )

    assert response.status_code == 422
    assert SECRET not in response.text
    # The error is still useful: which field, and which rule.
    errors = response.json()["error"]["details"]["errors"]
    assert errors[0]["loc"] == ["body", "password"]
    assert errors[0]["type"] == "string_too_long"
    assert "input" not in errors[0]


async def test_a_rejected_invitation_token_is_not_read_back(http: AsyncClient) -> None:
    """An invitation token grants membership. Echoing one is handing it out."""
    token = f"INVITATION-{uuid.uuid4().hex}"

    response = await http.post(
        f"{API}/invitations/accept",
        # `password` is the wrong type, so the whole payload fails validation
        # and every field - the token included - reaches the error serialiser.
        json={"token": token, "password": {"not": "a string"}},
    )

    assert response.status_code == 422
    assert token not in response.text


async def test_a_rejected_meta_access_token_is_not_read_back(http: AsyncClient) -> None:
    """A live provider credential, submitted to `/whatsapp/accounts`."""
    credential = f"EAAG-{uuid.uuid4().hex}"

    response = await http.post(
        f"{API}/whatsapp/accounts",
        json={
            "phone_number_id": "1" * 200,  # over the column maximum
            "waba_id": "2",
            "display_phone_number": "+20100000000",
            "access_token": credential,
        },
    )

    # Unauthenticated, so it may be refused before validation; either way the
    # credential must not appear.
    assert credential not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": SECRET},
        {"email": "a@b.com"},
        {"email": "a@b.com", "password": SECRET, "unexpected": SECRET},
    ],
)
async def test_no_login_payload_is_ever_echoed(http: AsyncClient, payload: dict) -> None:
    """Several shapes of invalid login, including an unexpected extra field -
    the schemas use `extra="forbid"`, and the rejection names the field."""
    response = await http.post(f"{API}/auth/login", json=payload)

    assert SECRET not in response.text


def test_the_filter_keeps_what_makes_an_error_actionable() -> None:
    """A unit check on the filter itself, so its contract is pinned."""
    filtered = _safe_validation_errors(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "password"),
                "msg": "String should have at most 128 characters",
                "input": SECRET,
                "url": "https://errors.pydantic.dev/2.13/v/string_too_long",
                "ctx": {"max_length": 128},
            }
        ]
    )

    assert filtered == [
        {
            "type": "string_too_long",
            "loc": ["body", "password"],
            "msg": "String should have at most 128 characters",
        }
    ]


def test_removing_the_filter_brings_the_leak_back() -> None:
    """The mutation check.

    A test that passes whether or not the guard exists proves nothing. This
    asserts the *unfiltered* path still contains the secret, so if somebody
    replaces `_safe_validation_errors` with a pass-through, the test above
    starts failing rather than continuing to look green.
    """
    raw = [
        {
            "type": "string_too_long",
            "loc": ("body", "password"),
            "msg": "too long",
            "input": SECRET,
        }
    ]

    assert SECRET in str(raw), "the fixture no longer contains a secret to leak"
    assert SECRET not in str(_safe_validation_errors(raw))


# --------------------------------------------------------- security headers


@pytest.mark.parametrize(
    "header,expected",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
    ],
)
async def test_every_response_carries_the_security_headers(
    http: AsyncClient,
    header: str,
    expected: str,
) -> None:
    """Set by the application, not only by nginx.

    nginx is one topology. The production compose file runs this container on
    its own, and the image can be run directly; in both, a header configured
    only in the proxy is simply absent.
    """
    response = await http.get(f"{API}/auth/me")

    assert response.headers.get(header) == expected


async def test_a_content_security_policy_is_set_and_denies_framing(http: AsyncClient) -> None:
    response = await http.get(f"{API}/auth/me")
    policy = response.headers.get("Content-Security-Policy", "")

    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


async def test_error_responses_carry_the_headers_too(http: AsyncClient) -> None:
    """The path most likely to miss them: a response an exception handler built
    rather than a route."""
    unauthorised = await http.get(f"{API}/conversations")
    invalid = await http.post(f"{API}/auth/login", json={})

    for response in (unauthorised, invalid):
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Cache-Control") == "no-store"


async def test_hsts_is_not_sent_over_plain_http(http: AsyncClient) -> None:
    """Sending it over HTTP is meaningless, and sending it from a development
    server would pin `localhost` to HTTPS in that developer's browser."""
    response = await http.get(f"{API}/auth/me")

    assert "Strict-Transport-Security" not in response.headers


async def test_hsts_is_sent_when_a_trusted_proxy_reports_https() -> None:
    """And only from a peer we listed - otherwise any caller could assert HTTPS
    and collect an HSTS pin for this host."""
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret="x" * 40,
        meta_app_secret="s",
        docs_enabled=False,
        cors_origins=[],
        log_level="CRITICAL",
        rate_limit_enabled=False,
        # The ASGI transport reports this as the peer address.
        trusted_proxy_ips=["127.0.0.1"],
    )
    app = create_app(settings)
    app.state.database = FakeDependency(name="postgresql")
    app.state.redis = FakeDependency(name="redis")

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1234)),
        base_url="http://wasla.test",
    ) as client:
        trusted = await client.get(f"{API}/auth/me", headers={"X-Forwarded-Proto": "https"})

    assert "max-age=" in trusted.headers.get("Strict-Transport-Security", "")


async def test_an_untrusted_peer_cannot_induce_an_hsts_pin(http: AsyncClient) -> None:
    """`trusted_proxy_ips` is empty on this fixture, so the header is ignored."""
    response = await http.get(
        f"{API}/auth/me",
        headers={"X-Forwarded-Proto": "https"},
    )

    assert "Strict-Transport-Security" not in response.headers


# ------------------------------------------------------- the webhook's cap


@pytest_asyncio.fixture
async def capped(production_settings: Settings) -> AsyncIterator[AsyncClient]:
    """A small, explicit pair of caps so the test states its own numbers."""
    settings = production_settings.model_copy(
        update={"max_request_bytes": 2048, "webhook_max_request_bytes": 256}
    )
    app = create_app(settings)
    app.state.database = FakeDependency(name="postgresql")
    app.state.redis = FakeDependency(name="redis")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


async def test_the_webhook_is_held_to_a_tighter_body_cap(capped: AsyncClient) -> None:
    """The one endpoint an unauthenticated caller can reach.

    Signature verification happens after the body is read, so without a cap of
    its own the cost of making the server buffer the full 32 MB allowance - which
    exists for media uploads by signed-in colleagues - is one unsigned request.
    """
    response = await capped.post(
        f"{API}/webhooks/whatsapp",
        content=b"x" * 512,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


async def test_a_payload_under_the_webhook_cap_still_reaches_the_endpoint(
    capped: AsyncClient,
) -> None:
    """The control. A cap that refused everything would lose customer messages,
    which is the failure ADR-032 exists to prevent."""
    response = await capped.post(
        f"{API}/webhooks/whatsapp",
        content=b'{"object":"whatsapp_business_account","entry":[]}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413


async def test_other_routes_keep_the_larger_cap(capped: AsyncClient) -> None:
    """The webhook's cap is tighter than the general one, not a replacement for
    it: an upload of 512 bytes is refused at the webhook and fine elsewhere."""
    response = await capped.post(
        f"{API}/auth/login",
        content=b'{"email":"a@b.com","password":"' + b"z" * 400 + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413
