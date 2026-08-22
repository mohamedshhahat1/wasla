"""The outbound client, against a mock transport.

The real client runs here: only the network is replaced. Retry counts, backoff
sleeps, URLs, headers and bodies are all asserted without a network call or a
wall-clock wait.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.integrations.whatsapp.client import WhatsAppClient

ACCESS_TOKEN = "meta-access-token"
PHONE_NUMBER_ID = "109876543210"
RECIPIENT = "201234567890"
# Built by concatenation on purpose: an f-string here needs literal braces
# around nothing, which is easy to get wrong and impossible to notice.
MESSAGES_URL = "https://graph.facebook.com/v21.0/" + PHONE_NUMBER_ID + "/messages"
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

    def body(self, index: int = 0):
        return json.loads(self.requests[index].content)


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
    assert str(request.url) == MESSAGES_URL
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_the_text_body_matches_the_cloud_api_contract():
    recorder = Recorder(_ok())

    await _client(recorder).send_text(phone_number_id=PHONE_NUMBER_ID, to=RECIPIENT, body="hi")

    assert recorder.body() == {
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
    assert recorder.sleeps == [0.1]


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

    # Meta rejects the whole message if audio carries a caption.
    assert "caption" not in recorder.body(0)["audio"]
    assert recorder.body(1)["image"]["caption"] == "kept"


async def test_a_document_keeps_its_filename():
    recorder = Recorder(_ok())

    await _client(recorder).send_media(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        kind="document",
        media_id="media-9",
        filename="invoice.pdf",
    )

    assert recorder.body()["document"] == {"id": "media-9", "filename": "invoice.pdf"}


async def test_a_template_carries_its_language_and_components():
    recorder = Recorder(_ok())

    await _client(recorder).send_template(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        name="order_update",
        language="ar",
        components=[{"type": "body", "parameters": [{"type": "text", "text": "123"}]}],
    )

    payload = recorder.body()
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


async def test_reply_buttons_are_shaped_as_meta_expects():
    recorder = Recorder(_ok())

    await _client(recorder).send_buttons(
        phone_number_id=PHONE_NUMBER_ID,
        to=RECIPIENT,
        body="pick one",
        buttons=[("yes", "Yes"), ("no", "No")],
    )

    interactive = recorder.body()["interactive"]
    assert interactive["type"] == "button"
    assert interactive["action"]["buttons"][0] == {
        "type": "reply",
        "reply": {"id": "yes", "title": "Yes"},
    }


async def test_marking_a_message_read_posts_a_status():
    recorder = Recorder(httpx.Response(200, json={"success": True}))

    await _client(recorder).mark_read(
        phone_number_id=PHONE_NUMBER_ID,
        message_id="wamid.in",
    )

    assert recorder.body() == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.in",
    }


async def test_a_client_without_a_token_refuses_to_exist():
    recorder = Recorder(_ok())

    # A missing platform credential is a misconfiguration, not a caller error.
    with pytest.raises(DependencyUnavailableError):
        WhatsAppClient(
            http=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler)),
            access_token="",
        )


MEDIA_ID = "media-handle-1"
MEDIA_URL = "https://graph.facebook.com/v21.0/" + MEDIA_ID
UPLOAD_URL = "https://graph.facebook.com/v21.0/" + PHONE_NUMBER_ID + "/media"
CDN_URL = "https://lookaside.fbsbx.com/whatsapp/1234"


def _descriptor(**overrides) -> httpx.Response:
    body = {
        "url": CDN_URL,
        "mime_type": "image/jpeg",
        "sha256": "abc123",
        "file_size": 2048,
        "id": MEDIA_ID,
        **overrides,
    }
    return httpx.Response(200, json=body)


async def test_a_file_is_fetched_in_two_steps():
    """The webhook carries a handle, not a file.

    Resolving it returns a short-lived CDN URL, and that URL is fetched
    separately - the two calls are why downloading belongs in a worker rather
    than on the webhook path.
    """
    recorder = Recorder(_descriptor(), httpx.Response(200, content=b"jpeg-bytes"))

    downloaded = await _client(recorder).fetch_media(MEDIA_ID)

    assert downloaded.content == b"jpeg-bytes"
    assert downloaded.mime_type == "image/jpeg"
    assert downloaded.byte_size == len(b"jpeg-bytes")
    assert downloaded.declared_size == 2048
    assert str(recorder.requests[0].url) == MEDIA_URL
    assert str(recorder.requests[1].url) == CDN_URL


async def test_the_cdn_url_is_still_fetched_with_the_token():
    """It looks public and is not. Without the header Meta answers 401."""
    recorder = Recorder(_descriptor(), httpx.Response(200, content=b"bytes"))

    await _client(recorder).fetch_media(MEDIA_ID)

    assert recorder.requests[1].headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_codec_parameters_are_stripped_from_a_fetched_type():
    recorder = Recorder(
        _descriptor(mime_type="audio/ogg; codecs=opus"),
        httpx.Response(200, content=b"ogg"),
    )

    downloaded = await _client(recorder).fetch_media(MEDIA_ID)

    assert downloaded.mime_type == "audio/ogg"


async def test_a_descriptor_without_a_location_is_refused():
    recorder = Recorder(_descriptor(url=None))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).fetch_media(MEDIA_ID)


async def test_a_read_is_retried_where_a_send_would_not_be():
    """The opposite policy to sending, deliberately.

    A repeated read costs a request and changes nothing anyone can see, so a
    5xx is worth another attempt here where it must never be on a send.
    """
    recorder = Recorder(
        httpx.Response(500),
        _descriptor(),
        httpx.Response(200, content=b"bytes"),
    )

    downloaded = await _client(recorder).fetch_media(MEDIA_ID)

    assert downloaded.content == b"bytes"
    assert recorder.attempts == 3


async def test_a_read_gives_up_after_the_attempt_budget():
    recorder = Recorder(httpx.Response(500))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).probe_media(MEDIA_ID)

    assert recorder.attempts == 3


async def test_a_rate_limited_read_reports_itself_as_such():
    recorder = Recorder(httpx.Response(429))

    with pytest.raises(RateLimitedError):
        await _client(recorder).probe_media(MEDIA_ID)


async def test_a_size_can_be_asked_for_without_moving_the_file():
    """The point of the cap: not paying to fetch what will be thrown away."""
    recorder = Recorder(_descriptor(file_size=90_000_000))

    descriptor = await _client(recorder).probe_media(MEDIA_ID)

    assert descriptor.byte_size == 90_000_000
    assert descriptor.mime_type == "image/jpeg"
    assert recorder.attempts == 1


async def test_an_upload_returns_the_id_it_can_be_sent_with():
    recorder = Recorder(httpx.Response(200, json={"id": "uploaded-1"}))

    media_id = await _client(recorder).upload_media(
        phone_number_id=PHONE_NUMBER_ID,
        content=b"%PDF-1.4",
        mime_type="application/pdf",
        filename="quote.pdf",
    )

    assert media_id == "uploaded-1"
    assert str(recorder.requests[0].url) == UPLOAD_URL
    assert b"quote.pdf" in recorder.requests[0].content
    assert b"whatsapp" in recorder.requests[0].content


async def test_an_upload_meta_will_not_identify_is_refused():
    """Accepted but unidentifiable is unusable: the send needs the id."""
    recorder = Recorder(httpx.Response(200, json={}))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).upload_media(
            phone_number_id=PHONE_NUMBER_ID,
            content=b"x",
            mime_type="image/png",
            filename="a.png",
        )


async def test_a_rejected_upload_does_not_leak_metas_error_text():
    """This client holds a live platform credential.

    Provider error text can echo fragments of the request, so only the status
    is logged and nothing of it reaches the caller.
    """
    recorder = Recorder(
        httpx.Response(400, json={"error": {"message": "Bad token abc123", "code": 190}})
    )

    with pytest.raises(ExternalServiceError) as raised:
        await _client(recorder).upload_media(
            phone_number_id=PHONE_NUMBER_ID,
            content=b"x",
            mime_type="image/png",
            filename="a.png",
        )

    assert "abc123" not in str(raised.value)
