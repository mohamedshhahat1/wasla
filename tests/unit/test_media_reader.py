"""Turning stored bytes into text.

Both providers are driven through `httpx.MockTransport`, so the requests that
would go to OpenAI are asserted rather than sent. What is worth pinning down
here is the shape of those requests - a data URL rather than a link, a filename
derived from the mime type rather than from the customer - and the difference
between a file that could not be read and one that had nothing in it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.transcription import TranscriptionClient
from app.services.extraction import UnreadableDocumentError
from app.services.media_reader import (
    MediaReader,
    ScannedDocumentError,
    SilentRecordingError,
)

PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _responses(handler: Callable[[httpx.Request], httpx.Response]) -> ResponsesClient:
    transport = httpx.MockTransport(handler)
    return ResponsesClient(
        http=httpx.AsyncClient(transport=transport),
        api_key="test-key",
        max_attempts=1,
    )


def _transcription(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model: str = "gpt-4o-mini-transcribe",
) -> TranscriptionClient:
    transport = httpx.MockTransport(handler)
    return TranscriptionClient(
        http=httpx.AsyncClient(transport=transport),
        api_key="test-key",
        model=model,
        max_attempts=1,
    )


def _reply(text: str) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


async def test_an_image_is_described() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply("A blue sofa with a price tag reading 4,500 EGP."))

    reader = MediaReader(responses=_responses(handler))
    result = await reader.read(content=PIXEL, mime_type="image/png")

    assert "4,500 EGP" in result.transcript
    assert result.method == "vision"


async def test_an_image_is_sent_as_a_data_url_not_a_link() -> None:
    """The alternative would put every customer's attachment behind a URL.

    That is a far wider exposure than handing the bytes to one request, so the
    image travels inline and nothing has to be publicly reachable.
    """
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json=_reply("A photograph."))

    await MediaReader(responses=_responses(handler)).read(content=PIXEL, mime_type="image/png")

    content = captured[0]["input"][0]["content"]
    parts = {part["type"] for part in content}
    assert parts == {"input_text", "input_image"}

    image = next(part for part in content if part["type"] == "input_image")
    assert image["image_url"].startswith("data:image/png;base64,")


async def test_a_model_that_says_nothing_is_an_error_not_an_empty_description() -> None:
    """Inventing a description would be worse than reporting the failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply("   "))

    with pytest.raises(ExternalServiceError):
        await MediaReader(responses=_responses(handler)).read(content=PIXEL, mime_type="image/jpeg")


async def test_a_voice_note_is_transcribed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "ممكن اعرف السعر؟", "language": "arabic"})

    result = await MediaReader(transcription=_transcription(handler)).read(
        content=b"ogg-bytes",
        mime_type="audio/ogg",
    )

    assert result.transcript == "ممكن اعرف السعر؟"
    assert result.method == "transcription"


async def test_the_upload_filename_comes_from_the_type_not_the_customer() -> None:
    """The provider needs a filename to infer the container format.

    Reusing the one the customer's phone sent would let an attacker choose it,
    so it is derived from the mime type instead.
    """
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return httpx.Response(200, json={"text": "hello"})

    await MediaReader(transcription=_transcription(handler)).read(
        content=b"mp3-bytes",
        mime_type="audio/mpeg",
    )

    assert b'filename="audio.mp3"' in captured[0]


async def test_no_language_is_forced_on_the_transcription() -> None:
    """Customers switch between Arabic and English inside one sentence.

    Pinning a language makes the other half come back as nonsense rather than
    as a translation, so the hint is left unset.
    """
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.read())
        return httpx.Response(200, json={"text": "hello"})

    await MediaReader(transcription=_transcription(handler)).read(
        content=b"ogg", mime_type="audio/ogg"
    )

    assert b'name="language"' not in captured[0]


async def test_silence_is_a_decision_not_a_failure() -> None:
    """The file was opened and there was nothing in it. Retrying finds the same."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    with pytest.raises(SilentRecordingError):
        await MediaReader(transcription=_transcription(handler)).read(
            content=b"ogg", mime_type="audio/ogg"
        )


async def test_a_plain_text_document_is_read_without_a_provider() -> None:
    result = await MediaReader().read(
        content=b"the warranty lasts two years", mime_type="text/plain"
    )

    assert result.transcript == "the warranty lasts two years"
    assert result.method == "extraction"


async def test_a_document_with_no_text_layer_is_a_decision() -> None:
    from tests.unit.test_extraction import _blank_pdf

    with pytest.raises(ScannedDocumentError):
        await MediaReader().read(content=_blank_pdf(), mime_type="application/pdf")


async def test_an_unsupported_type_is_refused() -> None:
    with pytest.raises(UnreadableDocumentError):
        await MediaReader().read(content=b"...", mime_type="application/zip")


async def test_reading_an_image_without_a_client_configured_fails_clearly() -> None:
    """A worker running documents alone should not need an API key.

    It should also not silently return nothing when handed an image.
    """
    with pytest.raises(ExternalServiceError):
        await MediaReader().read(content=PIXEL, mime_type="image/png")


def test_can_read_matches_what_read_accepts() -> None:
    reader = MediaReader()
    for mime_type in ("image/jpeg", "audio/ogg", "application/pdf", "text/plain"):
        assert reader.can_read(mime_type)
    for absent in ("application/zip", "video/mp4", ""):
        assert not reader.can_read(absent)
    assert not reader.can_read(None)
