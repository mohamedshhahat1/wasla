"""The embeddings client, against a mocked transport.

No network and no real waiting: the HTTP client, sleep function and attempt
budget are injected. What is under test is the request shape, the retry policy,
and the decoding - especially the ordering rule, because getting that wrong
would silently attach every chunk's meaning to its neighbour.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.integrations.openai.embeddings import EmbeddingsClient

MODEL = "text-embedding-3-small"
DIMENSIONS = 4


def _vector(seed: float) -> list[float]:
    return [seed, seed + 1, seed + 2, seed + 3]


def _body(vectors: Sequence[Sequence[float]], *, shuffle: bool = False) -> dict[str, Any]:
    data = [
        {"object": "embedding", "index": index, "embedding": vector}
        for index, vector in enumerate(vectors)
    ]
    if shuffle:
        data.reverse()
    return {"object": "list", "data": data, "model": MODEL}


class Recorder:
    """Captures requests and replies with a scripted sequence of responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if len(self._responses) > 1:
                return self._responses.pop(0)
            return self._responses[0]

        return httpx.MockTransport(handle)

    def payload(self, index: int = 0) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.requests[index].content)
        return payload


def _client(recorder: Recorder, *, attempts: int = 3) -> EmbeddingsClient:
    async def no_sleep(_seconds: float) -> None:
        return None

    return EmbeddingsClient(
        http=httpx.AsyncClient(transport=recorder.transport()),
        api_key="test-key",
        model=MODEL,
        dimensions=DIMENSIONS,
        max_attempts=attempts,
        sleep=no_sleep,
    )


def test_a_missing_api_key_is_our_misconfiguration_not_a_bad_request() -> None:
    with pytest.raises(DependencyUnavailableError):
        EmbeddingsClient(
            http=httpx.AsyncClient(),
            api_key="",
            model=MODEL,
            dimensions=DIMENSIONS,
        )


async def test_embedding_sends_the_model_input_and_width() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0)])))

    await _client(recorder).embed(["finishing prices"])

    payload = recorder.payload()
    assert payload["model"] == MODEL
    assert payload["input"] == ["finishing prices"]
    # Requested explicitly so the provider cannot return a width the column
    # will not accept.
    assert payload["dimensions"] == DIMENSIONS


async def test_the_api_key_travels_as_a_bearer_token() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0)])))

    await _client(recorder).embed(["text"])

    assert recorder.requests[0].headers["Authorization"] == "Bearer test-key"


async def test_an_empty_batch_makes_no_request() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([])))

    assert await _client(recorder).embed([]) == []
    assert recorder.requests == []


async def test_blank_input_is_refused_before_a_request_is_made() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0)])))

    with pytest.raises(ValidationError):
        await _client(recorder).embed(["fine", "   "])
    assert recorder.requests == []


async def test_vectors_come_back_in_input_order() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0), _vector(10.0)])))

    vectors = await _client(recorder).embed(["first", "second"])

    assert vectors == [_vector(0.0), _vector(10.0)]


async def test_vectors_are_sorted_by_the_providers_index() -> None:
    """The API documents an index because array order is not guaranteed.

    Trusting the array would attach each chunk's meaning to its neighbour.
    """
    recorder = Recorder(
        httpx.Response(200, json=_body([_vector(0.0), _vector(10.0)], shuffle=True))
    )

    vectors = await _client(recorder).embed(["first", "second"])

    assert vectors == [_vector(0.0), _vector(10.0)]


async def test_embed_one_returns_a_single_vector() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(5.0)])))

    assert await _client(recorder).embed_one("a question") == _vector(5.0)


async def test_a_wrong_number_of_vectors_is_refused() -> None:
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0)])))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).embed(["first", "second"])


async def test_a_wrong_width_is_refused_before_it_reaches_the_column() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]}))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).embed(["text"])


async def test_an_unreadable_body_is_refused() -> None:
    recorder = Recorder(httpx.Response(200, content=b"not json"))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).embed(["text"])


async def test_rate_limiting_is_retried_then_reported() -> None:
    recorder = Recorder(httpx.Response(429, json={"error": {"code": "rate_limit"}}))

    with pytest.raises(RateLimitedError):
        await _client(recorder, attempts=3).embed(["text"])

    assert len(recorder.requests) == 3


async def test_a_server_error_is_retried() -> None:
    recorder = Recorder(
        httpx.Response(503, json={"error": {"code": "unavailable"}}),
        httpx.Response(200, json=_body([_vector(1.0)])),
    )

    vectors = await _client(recorder).embed(["text"])

    assert vectors == [_vector(1.0)]
    assert len(recorder.requests) == 2


async def test_a_bad_request_is_not_retried() -> None:
    """Our request is wrong; repeating it will not help."""
    recorder = Recorder(httpx.Response(400, json={"error": {"code": "invalid_request"}}))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).embed(["text"])

    assert len(recorder.requests) == 1


async def test_a_transport_failure_is_retried_then_reported() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async def no_sleep(_seconds: float) -> None:
        return None

    client = EmbeddingsClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        api_key="test-key",
        model=MODEL,
        dimensions=DIMENSIONS,
        max_attempts=2,
        sleep=no_sleep,
    )

    with pytest.raises(ExternalServiceError):
        await client.embed(["text"])


async def test_a_large_batch_is_split_across_requests() -> None:
    """One request per chunk would turn ingestion into a rate-limit problem."""
    recorder = Recorder(httpx.Response(200, json=_body([_vector(0.0)] * 96)))

    await _client(recorder).embed(["text"] * 96)

    assert len(recorder.requests) == 1
    assert len(recorder.payload()["input"]) == 96
