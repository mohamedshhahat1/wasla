"""The five Embedded Signup Graph calls, against an in-process Graph (ADR-204, FK-04).

Asserted: each call's method, path, version and body are the ones Meta's
onboarding pages give (read 2026-11-21); the exchange is never retried; Meta's
refusals map to Wasla's codes; and nothing secret reaches a log line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.integrations.whatsapp.onboarding import (
    EmbeddedSignupCodeExpiredError,
    EmbeddedSignupCodeInvalidError,
    MetaOnboardingClient,
    OnboardingStepError,
)

SECRET = "es-app-secret-never-logged"
CODE = "es-code-never-logged"
TOKEN = "EAAB-es-business-token-never-logged"


class Graph:
    def __init__(self, answers: dict[tuple[str, str], tuple[int, Any]]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.get((request.method, request.url.path), (404, {}))
        return httpx.Response(status, json=body)


def _client(graph: Graph) -> MetaOnboardingClient:
    return MetaOnboardingClient(
        app_id="1234567890",
        app_secret=SECRET,
        api_version="v27.0",
        http=httpx.AsyncClient(transport=httpx.MockTransport(graph)),
    )


async def test_ex04_the_code_is_exchanged_once_with_the_apps_id_and_secret() -> None:
    graph = Graph(
        {
            ("GET", "/v27.0/oauth/access_token"): (
                200,
                {"access_token": TOKEN, "token_type": "bearer"},
            )
        }
    )

    token = await _client(graph).exchange_code(CODE)

    assert token.access_token == TOKEN
    assert token.expires_in is None
    (request,) = graph.requests
    assert request.url.host == "graph.facebook.com"
    assert dict(request.url.params) == {
        "client_id": "1234567890",
        "client_secret": SECRET,
        "code": CODE,
    }
    assert "Authorization" not in request.headers


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("This authorization code has expired.", EmbeddedSignupCodeExpiredError),
        ("This authorization code has been used.", EmbeddedSignupCodeInvalidError),
    ],
)
async def test_ex05_metas_refusal_of_the_code_is_mapped_and_not_retried(
    message: str, error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    graph = Graph(
        {
            ("GET", "/v27.0/oauth/access_token"): (
                400,
                {"error": {"message": message, "type": "OAuthException", "code": 100}},
            )
        }
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(error):
        await _client(graph).exchange_code(CODE)

    assert len(graph.requests) == 1
    assert SECRET not in caplog.text and CODE not in caplog.text


async def test_ex05_meta_unreachable_on_the_exchange_is_an_upstream_failure() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = MetaOnboardingClient(
        app_id="1",
        app_secret=SECRET,
        api_version="v27.0",
        http=httpx.AsyncClient(transport=httpx.MockTransport(down)),
    )

    with pytest.raises(ExternalServiceError):
        await client.exchange_code(CODE)


async def test_sb01_sb09_subscribe_and_register_send_metas_shapes() -> None:
    graph = Graph(
        {
            ("POST", "/v27.0/555000111/subscribed_apps"): (200, {"success": True}),
            ("POST", "/v27.0/109876543210/register"): (200, {"success": True}),
            ("DELETE", "/v27.0/555000111/subscribed_apps"): (200, {"success": True}),
        }
    )
    client = _client(graph)

    await client.subscribe(token=TOKEN, messaging_account_id="555000111")
    await client.register(token=TOKEN, phone_number_id="109876543210", pin="123456")
    await client.unsubscribe(token=TOKEN, messaging_account_id="555000111")

    subscribe, register, unsubscribe = graph.requests
    assert subscribe.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(register.content) == {"messaging_product": "whatsapp", "pin": "123456"}
    assert unsubscribe.method == "DELETE"
    # Never the Deregister API (RL-04).
    assert all(not request.url.path.endswith("/deregister") for request in graph.requests)


async def test_sb02_a_subscription_meta_did_not_confirm_is_a_failed_step() -> None:
    graph = Graph({("POST", "/v27.0/555000111/subscribed_apps"): (200, {"success": False})})

    with pytest.raises(OnboardingStepError) as failed:
        await _client(graph).subscribe(token=TOKEN, messaging_account_id="555000111")

    assert failed.value.step == "subscribe"


async def test_sb11_an_expired_token_is_an_auth_failure() -> None:
    graph = Graph(
        {
            ("POST", "/v27.0/109876543210/register"): (
                401,
                {"error": {"message": "Error validating access token", "code": 190}},
            )
        }
    )

    with pytest.raises(OnboardingStepError) as failed:
        await _client(graph).register(token=TOKEN, phone_number_id="109876543210", pin="000000")

    assert failed.value.is_auth


@pytest.mark.parametrize(
    ("answer", "coexistence"),
    [
        ({"is_on_biz_app": True, "platform_type": "CLOUD_API", "id": "1"}, True),
        ({"is_on_biz_app": False, "platform_type": "CLOUD_API", "id": "1"}, False),
        ({"is_on_biz_app": True, "platform_type": "NOT_APPLICABLE", "id": "1"}, False),
    ],
)
async def test_cx01_coexistence_is_metas_two_fields_together(
    answer: dict[str, Any], coexistence: bool
) -> None:
    graph = Graph({("GET", "/v27.0/109876543210"): (200, answer)})

    platform = await _client(graph).read_platform(token=TOKEN, phone_number_id="109876543210")

    assert platform.coexistence is coexistence
    assert graph.requests[0].url.params["fields"] == "is_on_biz_app,platform_type"


async def test_cx06_an_answer_without_is_on_biz_app_is_a_failed_read() -> None:
    graph = Graph({("GET", "/v27.0/109876543210"): (200, {"id": "109876543210"})})

    with pytest.raises(OnboardingStepError):
        await _client(graph).read_platform(token=TOKEN, phone_number_id="109876543210")


@pytest.mark.parametrize("sync_type", ["smb_app_state_sync", "history"])
async def test_sy01_a_sync_request_returns_metas_request_id(sync_type: str) -> None:
    graph = Graph(
        {
            ("POST", "/v27.0/109876543210/smb_app_data"): (
                200,
                {"messaging_product": "whatsapp", "request_id": "req-1"},
            )
        }
    )

    request_id = await _client(graph).request_sync(
        token=TOKEN, phone_number_id="109876543210", sync_type=sync_type
    )

    assert request_id == "req-1"
    assert json.loads(graph.requests[0].content) == {
        "messaging_product": "whatsapp",
        "sync_type": sync_type,
    }


async def test_an_unknown_sync_type_is_refused_before_meta() -> None:
    graph = Graph({})

    with pytest.raises(ValueError):
        await _client(graph).request_sync(token=TOKEN, phone_number_id="1", sync_type="everything")

    assert graph.requests == []
