"""Every way a media upload ends is a type a campaign can act on (HM-06, ADR-144).

A campaign uploads its header file once for everybody, so "Meta refused the
file", "Meta was not reached" and "Meta may hold it, with no id to use" lead to
three different things - fail, wait, upload again - and the client says which
by type. Every one stays an `ExternalServiceError`, so an attachment send,
which treats any upload failure as "nothing delivered", is unchanged.
"""

from __future__ import annotations

import httpx
import pytest

from app.channels.outcomes import (
    ProviderAuthError,
    ProviderConnectionRefusedError,
    ProviderUnreachableError,
    UncertainDeliveryError,
)
from app.core.exceptions import ExternalServiceError, RateLimitedError
from app.integrations.whatsapp.client import MediaUploadRefusedError, WhatsAppClient

PHONE_NUMBER_ID = "109876543210"


def _client(answer: httpx.Response | type[Exception]) -> WhatsAppClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, httpx.Response):
            return answer
        raise answer("harness", request=request)  # type: ignore[call-arg]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WhatsAppClient(http=http, access_token="t", api_version="v21.0")


def _error(status: int, code: int | None) -> httpx.Response:
    body: dict[str, object] = {"message": "harness", "type": "OAuthException"}
    if code is not None:
        body["code"] = code
    return httpx.Response(status, json={"error": body})


@pytest.mark.parametrize(
    ("answer", "raised"),
    [
        (httpx.ConnectError, ProviderUnreachableError),
        (httpx.ConnectTimeout, ProviderUnreachableError),
        (httpx.PoolTimeout, ProviderUnreachableError),
        (httpx.ReadTimeout, UncertainDeliveryError),
        (httpx.RemoteProtocolError, UncertainDeliveryError),
        (_error(429, 130429), RateLimitedError),
        (_error(401, 190), ProviderAuthError),
        (_error(400, 100), MediaUploadRefusedError),
        (_error(400, 131053), MediaUploadRefusedError),
        (_error(500, None), UncertainDeliveryError),
        (httpx.Response(200, json={}), UncertainDeliveryError),
        (httpx.Response(200, content=b"not json"), UncertainDeliveryError),
    ],
)
async def test_each_ending_is_its_own_type(
    answer: httpx.Response | type[Exception], raised: type[Exception]
) -> None:
    with pytest.raises(raised) as caught:
        await _client(answer).upload_media(
            phone_number_id=PHONE_NUMBER_ID,
            content=b"\x89PNG\r\n\x1a\n",
            mime_type="image/png",
            filename="a.png",
        )
    assert isinstance(caught.value, ExternalServiceError | RateLimitedError)


async def test_a_refusal_carries_metas_code_and_not_its_text() -> None:
    with pytest.raises(MediaUploadRefusedError) as caught:
        await _client(_error(400, 100)).upload_media(
            phone_number_id=PHONE_NUMBER_ID,
            content=b"x",
            mime_type="image/png",
            filename="a.png",
        )
    assert caught.value.provider_code == 100
    assert "harness" not in str(caught.value)


async def test_a_connection_level_code_is_the_connections_refusal() -> None:
    with pytest.raises(ProviderConnectionRefusedError) as caught:
        await _client(_error(403, 200)).upload_media(
            phone_number_id=PHONE_NUMBER_ID,
            content=b"x",
            mime_type="image/png",
            filename="a.png",
        )
    assert not isinstance(caught.value, ProviderAuthError)
