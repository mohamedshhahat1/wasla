"""The Resend adapter, driven through every failure class it classifies.

No network: a mock transport answers, which is the only way to exercise a
429 and a malformed body deterministically. Two properties are load-bearing
and are asserted rather than assumed - that the API key travels in the
Authorization header and nowhere else, and that a refusal is classified as
retryable or not by the adapter rather than by the worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations.email import build_email_provider
from app.integrations.email.base import EmailMessage, EmailSendState
from app.integrations.email.resend import RESEND_ENDPOINT, ResendEmailProvider
from tests.fakes import as_settings

API_KEY = "re_test_key_do_not_use"


def _message() -> EmailMessage:
    return EmailMessage(
        sender="no-reply@example.com",
        to=("person@example.com",),
        subject="A subject",
        text="A body.",
        html="<p>A body.</p>",
    )


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> ResendEmailProvider:
    return ResendEmailProvider(
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _captured(status_code: int = 200, body: Any = None) -> tuple[Any, ...]:
    """A provider plus the list its requests land in."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body if body is not None else {"id": "msg-1"})

    return _provider(handler), requests


async def test_a_success_returns_the_provider_message_id() -> None:
    provider, _ = _captured(body={"id": "msg-abc"})

    result = await provider.send(_message())

    assert result.state is EmailSendState.SENT
    assert result.provider_message_id == "msg-abc"
    assert result.provider == "resend"


async def test_the_api_key_travels_only_in_the_authorization_header() -> None:
    provider, requests = _captured()

    await provider.send(_message())

    request = requests[0]
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in str(request.url)
    assert API_KEY not in request.content.decode()


async def test_the_endpoint_is_https_and_fixed() -> None:
    provider, requests = _captured()

    await provider.send(_message())

    assert str(requests[0].url) == RESEND_ENDPOINT
    assert RESEND_ENDPOINT.startswith("https://")


async def test_an_idempotency_key_is_forwarded_when_given() -> None:
    provider, requests = _captured()

    await provider.send(_message(), idempotency_key="invitation:123")

    assert requests[0].headers["Idempotency-Key"] == "invitation:123"


async def test_no_idempotency_header_is_sent_when_none_is_given() -> None:
    provider, requests = _captured()

    await provider.send(_message())

    assert "Idempotency-Key" not in requests[0].headers


async def test_the_payload_carries_both_bodies_and_the_sender() -> None:
    provider, requests = _captured()

    await provider.send(_message())

    payload = json.loads(requests[0].content)
    assert payload["from"] == "no-reply@example.com"
    assert payload["to"] == ["person@example.com"]
    assert payload["subject"] == "A subject"
    assert payload["text"] == "A body."
    assert payload["html"] == "<p>A body.</p>"


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_a_rate_limit_or_server_error_is_transient(status_code: int) -> None:
    provider, _ = _captured(status_code=status_code, body={"message": "later"})

    result = await provider.send(_message())

    assert result.state is EmailSendState.TRANSIENT_FAILURE


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
async def test_a_client_error_is_permanent(status_code: int) -> None:
    provider, _ = _captured(status_code=status_code, body={"message": "no"})

    result = await provider.send(_message())

    assert result.state is EmailSendState.PERMANENT_FAILURE


async def test_a_timeout_is_transient_and_names_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    result = await _provider(handler).send(_message())

    assert result.state is EmailSendState.TRANSIENT_FAILURE
    assert result.error_code == "timeout"


async def test_a_transport_error_is_transient_and_keeps_only_the_type() -> None:
    """httpx errors quote the request they carried, and it is credentialed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await _provider(handler).send(_message())

    assert result.state is EmailSendState.TRANSIENT_FAILURE
    assert result.error_code == "transport_error"
    assert result.error_message == "ConnectError"
    assert API_KEY not in (result.error_message or "")


async def test_a_success_with_an_unparseable_body_still_succeeds() -> None:
    """Acceptance is the status code; the id is a bonus, not a requirement."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    result = await _provider(handler).send(_message())

    assert result.state is EmailSendState.SENT
    assert result.provider_message_id is None


async def test_a_success_with_a_non_object_body_yields_no_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    result = await _provider(handler).send(_message())

    assert result.state is EmailSendState.SENT
    assert result.provider_message_id is None


async def test_a_failure_body_is_truncated_before_it_is_stored() -> None:
    """A provider that quotes the request back must not quote it all the way."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"name": "bad_request", "message": "x" * 5000})

    result = await _provider(handler).send(_message())

    assert result.error_code == "bad_request"
    assert result.error_message is not None
    assert len(result.error_message) <= 300


async def test_an_error_name_is_bounded_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"name": "n" * 5000})

    result = await _provider(handler).send(_message())

    assert len(result.error_code or "") <= 100


async def test_a_failure_without_a_json_body_still_classifies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<html>gateway</html>")

    result = await _provider(handler).send(_message())

    assert result.state is EmailSendState.TRANSIENT_FAILURE
    assert result.error_code == "http_503"


class _Settings:
    """Only what `build_email_provider` reads."""

    def __init__(
        self,
        *,
        provider: str = "resend",
        api_key: str | None = None,
    ) -> None:
        self.email_provider = provider
        self.resend_api_key = api_key


def test_building_a_resend_provider_without_a_key_refuses() -> None:
    """The sending process must not start believing it can send."""
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        build_email_provider(as_settings(_Settings(api_key=None)))


def test_building_a_resend_provider_with_a_key_succeeds() -> None:
    provider = build_email_provider(as_settings(_Settings(api_key=API_KEY)))

    assert provider.name == "resend"


def test_building_the_fake_needs_no_key() -> None:
    assert build_email_provider(as_settings(_Settings(provider="fake"))).name == "fake"
