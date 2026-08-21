"""The outbound client, against a mock transport.

The real client runs here: only the network is replaced. Retry counts, backoff
sleeps, URLs, headers and bodies are all asserted without a network call or a
wall-clock wait.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.integrations.whatsapp.client import WhatsAppClient

ACCESS_TOKEN = "meta-access-token"
PHONE_NUMBER_ID = "109876543210"
RECIPIENT = "201234567890"
ACCEPTED = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": RECIPIENT, "wa_id": RECIPIENT}],
    "messages": [{"id": "wamid.sent"}],
}


class Recorder:
    """Captures requests and replays a scripted sequence of responses."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    @property
    def attempts(self) -> int:
        return len(self.requests)


def _client(recorder: Recorder, **overrides) -> WhatsAppClient:
    return WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler)),
        access_token=ACCESS_TOKEN,
        sleep=recorder.sleep,
        backoff_seconds=0.1,
        **overrides,
    )


def _ok() -> httpx.Response:
    return httpx.Response(200, json=ACCEPTED)


async def test_a_text_message_is_posted_where_meta_expects_it():
    recorder = Recorder(_ok())

    sent = await _client(recorder).send_text(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        body="hello",
    )

    assert sent.message_id == "wamid.sent"
    assert sent.recipient == RECIPIENT

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    )
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_the_text_body_matches_the_cloud_api_contract():
    recorder = Recorder(_ok())

    await _client(recorder).send_text(phone_number_id=PHONE_NUMBER_ID, to=RECIPIENT, body="hi")

    import json

    payload = json.loads(recorder.requests[0].content)
    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": RECIPIENT,
        "type": "text",
        "text": {"body": "hi", "preview_url": False},
    }


async def test_rate_limiting_is_retried_and_then_succeeds():
    recorder = Recorder(httpx.Response(429), _ok())

    sent = await _client(recorder).send_text(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        body="hello",
    )

    assert sent.message_id == "wamid.sent"
    assert recorder.attempts == 2
    assert recorder.sleeps == [0.1]


async def test_persistent_rate_limiting_raises_after_the_attempt_budget():
    recorder = Recorder(httpx.Response(429))

    with pytest.raises(RateLimitedError):
        await _client(recorder).send_text(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="hello",
        )

    assert recorder.attempts == 3


async def test_a_server_error_is_not_retried():
    recorder = Recorder(httpx.Response(500, json={"error": {"code": 1, "type": "OAuthException"}}))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).send_text(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="hello",
        )

    # The message may already have been accepted; a retry could send it twice.
    assert recorder.attempts == 1
    assert recorder.sleeps == []


async def test_a_rejected_request_never_leaks_the_token_or_meta_text():
    recorder = Recorder(
        httpx.Response(
            400,
            json={"error": {"message": "Invalid parameter secret-ish text", "code": 100}},
        )
    )

    with pytest.raises(ExternalServiceError) as raised:
        await _client(recorder).send_text(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="hello",
        )

    message = str(raised.value)
    assert ACCESS_TOKEN not in message
    assert "secret-ish" not in message


async def test_a_connection_error_is_retried():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("no route", request=request)
        return _ok()

    recorder = Recorder(_ok())
    client = WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        access_token=ACCESS_TOKEN,
        sleep=recorder.sleep,
        backoff_seconds=0.1,
    )

    sent = await client.send_text(phone_number_id=PHONE_NUMBER_ID, to=RECIPIENT, body="hello")

    assert sent.message_id == "wamid.sent"
    assert attempts["count"] == 2


async def test_a_timeout_is_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    recorder = Recorder(_ok())
    client = WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        access_token=ACCESS_TOKEN,
        sleep=recorder.sleep,
    )

    with pytest.raises(ExternalServiceError):
        await client.send_text(phone_number_id=PHONE_NUMBER_ID, to=RECIPIENT, body="hello")

    assert recorder.sleeps == []


async def test_an_acceptance_without_a_message_id_is_an_error():
    recorder = Recorder(httpx.Response(200, json={"messaging_product": "whatsapp"}))

    # Delivery statuses arrive keyed on the id, so a message we cannot identify
    # is not usable.
    with pytest.raises(ExternalServiceError):
        await _client(recorder).send_text(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="hello",
        )


async def test_media_needs_exactly_one_source():
    recorder = Recorder(_ok())
    client = _client(recorder)

    with pytest.raises(ValidationError):
        await client.send_media(phone_number_id=PHONE_NUMBER_ID, to=RECIPIENT, kind="image")

    with pytest.raises(ValidationError):
        await client.send_media(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            kind="image",
            link="https://example.test/a.jpg",
            media_id="media-1",
        )

    assert recorder.attempts == 0


async def test_a_caption_is_dropped_for_audio_and_kept_for_images():
    import json

    recorder = Recorder(_ok())
    client = _client(recorder)

    await client.send_media(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        kind="audio",
        link="https://example.test/a.ogg",
        caption="ignored",
    )
    await client.send_media(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        kind="image",
        link="https://example.test/a.jpg",
        caption="kept",
    )

    audio = json.loads(recorder.requests[0].content)
    image = json.loads(recorder.requests[1].content)
    # Meta rejects the whole message if audio carries a caption.
    assert "caption" not in audio["audio"]
    assert image["image"]["caption"] == "kept"


async def test_a_template_carries_its_language_and_components():
    import json

    recorder = Recorder(_ok())

    await _client(recorder).send_template(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        name="order_update",
        language="ar",
        components=[{"type": "body", "parameters": [{"type": "text", "text": "123"}]}],
    )

    payload = json.loads(recorder.requests[0].content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "order_update"
    assert payload["template"]["language"] == {"code": "ar"}
    assert payload["template"]["components"][0]["type"] == "body"


async def test_buttons_are_limited_to_three():
    recorder = Recorder(_ok())
    client = _client(recorder)

    with pytest.raises(ValidationError):
        await client.send_buttons(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="pick",
            buttons=[("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")],
        )

    with pytest.raises(ValidationError):
        await client.send_buttons(
            phone_number_id=PHONE_NUMBER_ID,
            to=RECIPIENT,
            body="pick",
            buttons=[],
        )

    assert recorder.attempts == 0


async def test_marking_a_message_read_posts_a_status():
    import json

    recorder = Recorder(httpx.Response(200, json={"success": True}))

    await _client(recorder).mark_read(
        phone_number_id=PHONE_NUMBER_ID,
        message_id="wamid.in",
    )

    payload = json.loads(recorder.requests[0].content)
    assert payload == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.in",
    }


async def test_a_client_without_a_token_refuses_to_exist():
    recorder = Recorder(_ok())

    with pytest.raises(ValidationError):
        WhatsAppClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler)),
            access_token="",
        )
