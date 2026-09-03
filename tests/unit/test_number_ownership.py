"""Proving control of a WhatsApp number, against a fake Graph API.

Adversarial by construction. Every test here is a way a claim could be waved
through by a verifier that looked reasonable: a token that reads a *different*
number, a business account the caller asserted rather than one Meta named, a
reply that is valid JSON and means nothing, an outage answered optimistically.

Nothing in this file touches the network. The transport is an httpx mock, so the
failure modes that matter - a timeout, a 401, a redirect, a body that is a list
- are exercised exactly rather than approximated.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations.whatsapp.ownership import (
    MetaOwnershipVerifier,
    NumberOwnershipError,
)

NUMBER = "109876543210"
WABA = "555000111"
TOKEN = "EAAG-the-workspaces-own-credential"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _number_node(**overrides: Any) -> dict[str, Any]:
    node = {
        "id": NUMBER,
        "display_phone_number": "+20 100 000 0000",
        "verified_name": "Acme Ltd",
        "whatsapp_business_account": {"id": WABA},
    }
    node.update(overrides)
    return node


class _Responder:
    """Answers with a fixed body, and remembers what it was asked.

    A class rather than a function carrying an attribute: the requests it
    collected are the thing most of these tests assert on, and a function
    with a list stapled to it cannot say so in its type.
    """

    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._body, str):
            return httpx.Response(self._status, text=self._body)
        return httpx.Response(self._status, json=self._body)


def _responder(body: Any, status: int = 200) -> _Responder:
    return _Responder(body, status)


async def test_a_credential_that_reads_the_number_proves_the_claim() -> None:
    handler = _responder(_number_node())
    verifier = MetaOwnershipVerifier(http=_client(handler))

    verified = await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)

    assert verified.phone_number_id == NUMBER
    assert verified.waba_id == WABA
    assert verified.display_phone_number == "+20 100 000 0000"
    assert verified.verified_name == "Acme Ltd"


async def test_the_credential_travels_as_a_bearer_token_and_nowhere_else() -> None:
    """Not in the query string, where it would land in Meta's access logs and
    in any proxy between here and there."""
    handler = _responder(_number_node())
    verifier = MetaOwnershipVerifier(http=_client(handler))

    await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)

    request = handler.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)


async def test_a_node_answering_to_a_different_id_is_refused() -> None:
    """The identity check. Graph resolves some aliases, and a 200 for a
    *different* number is not proof of the number being claimed."""
    handler = _responder(_number_node(id="999999999999"))
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


async def test_a_business_account_the_caller_asserted_wrongly_is_refused() -> None:
    """Refused rather than silently corrected. A mismatch means the person
    connecting believes something untrue about where this number sits."""
    handler = _responder(_number_node())
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(
            access_token=TOKEN,
            phone_number_id=NUMBER,
            claimed_waba_id="a-different-business",
        )


async def test_the_business_account_comes_from_meta_not_from_the_request() -> None:
    handler = _responder(_number_node(whatsapp_business_account={"id": "the-real-one"}))
    verifier = MetaOwnershipVerifier(http=_client(handler))

    verified = await verifier.verify(
        access_token=TOKEN,
        phone_number_id=NUMBER,
        claimed_waba_id="the-real-one",
    )

    assert verified.waba_id == "the-real-one"


async def test_a_number_node_without_a_business_account_falls_back_to_the_listing() -> None:
    """Some Graph versions omit the edge. The fallback inverts the question -
    list the claimed account's numbers and require this one to be among them -
    which still needs a token with access to that account."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": [{"id": "another"}, {"id": NUMBER}]})
        return httpx.Response(200, json=_number_node(whatsapp_business_account=None))

    verifier = MetaOwnershipVerifier(http=_client(handler))

    verified = await verifier.verify(
        access_token=TOKEN,
        phone_number_id=NUMBER,
        claimed_waba_id=WABA,
    )

    assert verified.waba_id == WABA


async def test_the_fallback_refuses_a_number_that_is_not_on_the_claimed_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/phone_numbers"):
            return httpx.Response(200, json={"data": [{"id": "somebody-elses"}]})
        return httpx.Response(200, json=_number_node(whatsapp_business_account=None))

    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(
            access_token=TOKEN,
            phone_number_id=NUMBER,
            claimed_waba_id=WABA,
        )


async def test_no_business_account_anywhere_fails_closed() -> None:
    """Nothing returned and nothing to check against. The relationship is
    unproven, so the claim fails rather than being stored with a blank."""
    handler = _responder(_number_node(whatsapp_business_account=None))
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
async def test_every_rejection_from_meta_is_the_same_refusal(status: int) -> None:
    handler = _responder({"error": {"code": 190, "message": "Invalid OAuth token"}}, status)
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


async def test_metas_error_text_never_reaches_the_caller() -> None:
    """Provider error strings quote the request back, and this request carries
    a live credential."""
    handler = _responder(
        {"error": {"message": f"Invalid token {TOKEN}", "code": 190}},
        401,
    )
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError) as raised:
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)

    assert TOKEN not in str(raised.value)
    assert "190" not in str(raised.value)


async def test_a_timeout_is_a_refusal_rather_than_a_pass() -> None:
    """The failure that would be most tempting to answer optimistically. A
    verification that did not complete did not succeed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


async def test_a_connection_failure_is_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        json.dumps([{"id": NUMBER}]),
        json.dumps("a bare string"),
        json.dumps(None),
    ],
)
async def test_a_malformed_reply_is_a_refusal(body: Any) -> None:
    handler = _responder(body)
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


@pytest.mark.parametrize("value", [None, "", 12345, {"nested": "object"}])
async def test_a_non_string_identifier_is_not_a_match(
    value: dict[str, Any] | int | str | None,
) -> None:
    """`node["id"] == phone_number_id` is only a check if the type is checked
    too - otherwise a reply shaped `{"id": null}` for a number claimed as the
    string "None" would compare interestingly."""
    handler = _responder(_number_node(id=value))
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


async def test_a_node_without_a_display_number_is_refused() -> None:
    """Every real phone number node carries one. Its absence means this is not
    the object it claims to be - and the alternative, falling back to whatever
    the request said, is exactly the spoofing this closes."""
    handler = _responder(_number_node(display_phone_number=None))
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)


async def test_an_empty_credential_is_refused_without_asking_meta() -> None:
    handler = _responder(_number_node())
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token="   ", phone_number_id=NUMBER)

    assert handler.requests == []


async def test_a_redirect_is_not_followed() -> None:
    """Every URL here is built from the Graph base. A node that answers with a
    redirect is not something to chase, and following one would take a request
    carrying a live credential somewhere Meta did not choose."""
    handler = _responder("", 302)
    verifier = MetaOwnershipVerifier(http=_client(handler))

    with pytest.raises(NumberOwnershipError):
        await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)

    assert len(handler.requests) == 1


async def test_the_request_goes_to_the_graph_api_at_the_configured_version() -> None:
    handler = _responder(_number_node())
    verifier = MetaOwnershipVerifier(http=_client(handler), api_version="v19.0")

    await verifier.verify(access_token=TOKEN, phone_number_id=NUMBER)

    url = handler.requests[0].url
    assert url.host == "graph.facebook.com"
    assert url.scheme == "https"
    assert url.path == f"/v19.0/{NUMBER}"
