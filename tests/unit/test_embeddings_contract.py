"""The embeddings client's provider contract, byte for byte (RAG-05, RAG-11, RAG-16).

Real `httpx` serialization against a deterministic transport: what is asserted is
the request the application actually builds and how it reads every answer the
provider - or a proxy in front of it - can give. No credential, no network, no
real waiting.

Each failure class pins two facts, because the ingestion worker decides from the
second one whether a document's retry budget is worth spending (RAG-01): what the
client did (retried or not, how many requests), and whether the error it raised
says `permanent`.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

import app.core.telemetry as telemetry
from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.core.logging import JsonFormatter
from app.core.telemetry import CallOutcome
from app.integrations.openai.embeddings import (
    EMBED_INGEST,
    EMBED_QUERY,
    EMBEDDINGS_PATH,
    MAX_BATCH,
    MAX_RETRY_AFTER_SECONDS,
    OPENAI_BASE_URL,
    EmbeddingError,
    EmbeddingFailure,
    EmbeddingRateLimitedError,
    EmbeddingsClient,
    validate_vector,
)

MODEL = "text-embedding-3-small"
WIDTH = 8
SENTINEL_KEY = "sk-RAG-SENTINEL-9f8e7d6c5b4a"


def _vector(seed: float = 0.5) -> list[float]:
    return [seed + index for index in range(WIDTH)]


def _body(count: int, *, vector: Any = None, usage: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector or _vector(index + 1)}
            for index in range(count)
        ],
        "model": MODEL,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _raw(count: int, element: str) -> bytes:
    """A body whose vector elements are written literally - NaN, true, null, "1"."""
    items = ",".join(
        f'{{"object":"embedding","index":{index},"embedding":[{",".join([element] * WIDTH)}]}}'
        for index in range(count)
    )
    return f'{{"object":"list","data":[{items}]}}'.encode()


class Recorder:
    """Captures requests and answers each with the next scripted response."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            answer = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return httpx.MockTransport(handle)

    def payload(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _client(
    recorder: Recorder,
    *,
    sleeps: Sleeps | None = None,
    jitter: float = 0.5,
    api_key: str = "sk-test-not-real",
    operation: str = EMBED_INGEST,
    attempts: int = 3,
) -> EmbeddingsClient:
    return EmbeddingsClient(
        http=httpx.AsyncClient(transport=recorder.transport()),
        api_key=api_key,
        model=MODEL,
        dimensions=WIDTH,
        max_attempts=attempts,
        sleep=sleeps or Sleeps(),
        jitter=lambda: jitter,
        operation=operation,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[str, str]]]:
    """Every attempt and every call the client reports, as (operation, outcome)."""
    seen: dict[str, list[tuple[str, str]]] = {"attempts": [], "calls": []}

    async def attempt(*, provider: Any, operation: str, outcome: CallOutcome) -> None:
        seen["attempts"].append((operation, str(outcome)))

    async def call(
        *, provider: Any, operation: str, outcome: CallOutcome, duration_seconds: Any = None
    ) -> None:
        seen["calls"].append((operation, str(outcome)))

    monkeypatch.setattr(telemetry, "record_provider_attempt", attempt)
    monkeypatch.setattr(telemetry, "record_provider_call", call)
    return seen


# ------------------------------------------------------------------ the request


async def test_the_request_goes_to_the_embeddings_endpoint_with_the_contract_body() -> None:
    recorder = Recorder(httpx.Response(200, json=_body(2)))

    await _client(recorder).embed(["first", "second"])

    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == OPENAI_BASE_URL + EMBEDDINGS_PATH
    assert request.headers["authorization"] == "Bearer sk-test-not-real"
    assert request.headers["content-type"] == "application/json"
    assert recorder.payload() == {
        "model": MODEL,
        "input": ["first", "second"],
        "dimensions": WIDTH,
        "encoding_format": "float",
    }


async def test_a_long_input_list_is_split_into_provider_sized_batches() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body(len(json.loads(request.content)["input"])))

    sizes: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sizes.append(len(json.loads(request.content)["input"]))
        return answer(request)

    client = EmbeddingsClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        api_key="k",
        model=MODEL,
        dimensions=WIDTH,
    )
    vectors = await client.embed([f"text {n}" for n in range(MAX_BATCH * 2 + 3)])

    assert sizes == [MAX_BATCH, MAX_BATCH, 3]
    assert len(vectors) == MAX_BATCH * 2 + 3


async def test_one_request_may_not_carry_more_than_a_batch() -> None:
    recorder = Recorder(httpx.Response(200, json=_body(1)))

    with pytest.raises(ValidationError):
        await _client(recorder).embed_batch(["x"] * (MAX_BATCH + 1))

    assert recorder.requests == []


def test_the_client_names_the_space_its_vectors_belong_to() -> None:
    client = _client(Recorder(httpx.Response(200, json=_body(1))))

    assert client.space == EmbeddingSpace(provider="openai", model=MODEL, dimensions=WIDTH)


async def test_usage_is_read_when_the_provider_reports_it() -> None:
    usage = {"prompt_tokens": 17, "total_tokens": 17}
    batch = await _client(Recorder(httpx.Response(200, json=_body(1, usage=usage)))).embed_batch(
        ["hello"]
    )

    assert batch.input_tokens == 17
    assert batch.characters == 5


@pytest.mark.parametrize(
    "usage", [None, {}, {"prompt_tokens": -1}, {"prompt_tokens": True}, {"prompt_tokens": "9"}]
)
async def test_unusable_usage_is_absent_rather_than_invented(usage: Any) -> None:
    body = _body(1)
    if usage is not None:
        body["usage"] = usage
    batch = await _client(Recorder(httpx.Response(200, json=body))).embed_batch(["hello"])

    assert batch.input_tokens is None


# ------------------------------------------------------------- the vectors


async def test_vectors_are_returned_in_the_order_the_provider_indexed_them() -> None:
    body = _body(3)
    body["data"].reverse()

    vectors = await _client(Recorder(httpx.Response(200, json=body))).embed(["a", "b", "c"])

    assert vectors == [_vector(1), _vector(2), _vector(3)]


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (json.dumps(_body(1, vector=[0.1] * (WIDTH - 1))).encode(), "width minus one"),
        (json.dumps(_body(1, vector=[0.1] * (WIDTH + 1))).encode(), "width plus one"),
        (json.dumps({"data": []}).encode(), "no vectors"),
        (json.dumps(_body(2)).encode(), "too many vectors"),
        (_raw(1, "NaN"), "NaN"),
        (_raw(1, "Infinity"), "Infinity"),
        (_raw(1, "-Infinity"), "negative Infinity"),
        (_raw(1, "null"), "null elements"),
        (_raw(1, "true"), "booleans"),
        (_raw(1, '"0.5"'), "numeric strings"),
        (_raw(1, "0"), "zero vector"),
        (_raw(1, "0.0"), "zero vector of floats"),
        (json.dumps({"data": [{"index": 0, "embedding": "0.1,0.2"}]}).encode(), "not a list"),
        (json.dumps({"data": ["not an object"]}).encode(), "item not an object"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_a_malformed_vector_is_a_permanent_failure_before_anything_stores_it(
    calls: dict[str, list[tuple[str, str]]], content: bytes, why: str
) -> None:
    recorder = Recorder(httpx.Response(200, content=content))

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["one text"])

    assert raised.value.failure is EmbeddingFailure.INVALID_EMBEDDING, why
    assert raised.value.permanent is True
    assert len(recorder.requests) == 1, "a malformed answer is not retried"
    assert calls["attempts"] == [(EMBED_INGEST, "failure")]
    assert calls["calls"] == [(EMBED_INGEST, "failure")]


async def test_duplicate_provider_indexes_are_refused() -> None:
    body = _body(2)
    body["data"][1]["index"] = 0

    with pytest.raises(EmbeddingError) as raised:
        await _client(Recorder(httpx.Response(200, json=body))).embed(["a", "b"])

    assert raised.value.failure is EmbeddingFailure.INVALID_EMBEDDING


@pytest.mark.parametrize(
    "value",
    [[1, 0, 0], [0.5, 0.5, -0.5], [1e-3, 0.0, 0.0]],
)
def test_real_finite_non_zero_numbers_are_accepted(value: list[float]) -> None:
    assert validate_vector(value, dimensions=3) == [float(v) for v in value]


@pytest.mark.parametrize(
    "value",
    [
        [True, 0.0, 0.0],
        ["1", 0.0, 0.0],
        [math.nan, 1.0, 0.0],
        [math.inf, 1.0, 0.0],
        [None, 1.0, 0.0],
        [0, 0, 0],
        [1e-300, 0.0, 0.0],
        [1.0, 0.0],
        (1.0, 0.0, 0.0),
    ],
)
def test_anything_else_is_refused(value: Any) -> None:
    with pytest.raises(EmbeddingError):
        validate_vector(value, dimensions=3)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b""),
        httpx.Response(200, content=b"{not json"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, content=b"<html>ok</html>", headers={"content-type": "text/html"}),
    ],
    ids=["empty", "malformed", "list", "html"],
)
async def test_an_unreadable_body_is_transient_and_not_retried_by_the_client(
    response: httpx.Response,
) -> None:
    recorder = Recorder(response)

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["x"])

    assert raised.value.failure is EmbeddingFailure.UNREADABLE_RESPONSE
    assert raised.value.permanent is False
    assert len(recorder.requests) == 1


# ------------------------------------------------------------ how much is read


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, *, chunk: bytes, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.pulled += len(self._chunk)
            yield self._chunk


async def test_a_declared_oversized_body_is_refused_without_being_read() -> None:
    # One input may be answered with at most WIDTH * 32 bytes + 64 KiB.
    body = json.dumps({"data": [], "padding": "x" * 200_000}).encode()
    recorder = Recorder(httpx.Response(200, content=body))

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["x"])

    assert raised.value.failure is EmbeddingFailure.RESPONSE_TOO_LARGE
    assert raised.value.permanent is True
    assert len(recorder.requests) == 1


async def test_a_streamed_oversized_body_is_abandoned_at_the_limit() -> None:
    stream = _CountingStream(chunk=b"x" * 16_384, chunks=200)  # 3.2 MB on offer
    recorder = Recorder(httpx.Response(200, stream=stream))

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["x"])

    limit = WIDTH * 32 + 65_536
    assert raised.value.failure is EmbeddingFailure.RESPONSE_TOO_LARGE
    assert 0 < stream.pulled <= limit + 16_384, "reading stopped at the limit"


async def test_the_limit_scales_with_the_batch_so_a_full_batch_is_readable() -> None:
    wide = 1536
    body = json.dumps(
        {
            "data": [
                {"index": index, "embedding": [0.012345678901234567] * wide}
                for index in range(MAX_BATCH)
            ]
        }
    ).encode()
    assert len(body) > 1_048_576, "a real full batch is larger than the Responses client's cap"
    client = EmbeddingsClient(
        http=httpx.AsyncClient(transport=Recorder(httpx.Response(200, content=body)).transport()),
        api_key="k",
        model=MODEL,
        dimensions=wide,
    )

    batch = await client.embed_batch([f"t{n}" for n in range(MAX_BATCH)])

    assert len(batch.vectors) == MAX_BATCH


# ------------------------------------------------------------ provider errors


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (400, EmbeddingFailure.PROVIDER_INVALID_REQUEST),
        (401, EmbeddingFailure.PROVIDER_UNAUTHORIZED),
        (403, EmbeddingFailure.PROVIDER_FORBIDDEN),
        (404, EmbeddingFailure.PROVIDER_MODEL_NOT_FOUND),
        (422, EmbeddingFailure.PROVIDER_INVALID_REQUEST),
    ],
)
async def test_a_client_error_is_permanent_and_never_retried(
    calls: dict[str, list[tuple[str, str]]], status: int, failure: EmbeddingFailure
) -> None:
    sleeps = Sleeps()
    recorder = Recorder(httpx.Response(status, json={"error": {"code": "whatever"}}))

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder, sleeps=sleeps).embed_batch(["x"])

    assert raised.value.failure is failure
    assert raised.value.permanent is True
    assert isinstance(raised.value, ExternalServiceError)
    assert len(recorder.requests) == 1
    assert sleeps.waits == []
    assert calls["attempts"] == [(EMBED_INGEST, "failure")]
    assert calls["calls"] == [(EMBED_INGEST, "failure")]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_error_is_retried_then_transient(
    calls: dict[str, list[tuple[str, str]]], status: int
) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": {"code": "server"}}))

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["x"])

    assert raised.value.failure is EmbeddingFailure.PROVIDER_UNAVAILABLE
    assert raised.value.permanent is False
    assert len(recorder.requests) == 3
    assert calls["attempts"] == [(EMBED_INGEST, "unavailable")] * 3
    assert calls["calls"] == [(EMBED_INGEST, "unavailable")]


async def test_a_server_error_that_clears_succeeds_and_counts_every_attempt(
    calls: dict[str, list[tuple[str, str]]],
) -> None:
    recorder = Recorder(
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(200, json=_body(1)),
    )

    batch = await _client(recorder, operation=EMBED_QUERY).embed_batch(["x"])

    assert len(batch.vectors) == 1
    assert calls["attempts"] == [
        (EMBED_QUERY, "unavailable"),
        (EMBED_QUERY, "rate_limited"),
        (EMBED_QUERY, "success"),
    ]
    assert calls["calls"] == [(EMBED_QUERY, "success")]


async def test_rate_limiting_that_persists_is_a_transient_rate_limit() -> None:
    recorder = Recorder(httpx.Response(429))

    with pytest.raises(EmbeddingRateLimitedError) as raised:
        await _client(recorder).embed_batch(["x"])

    assert isinstance(raised.value, RateLimitedError)
    assert raised.value.permanent is False
    assert len(recorder.requests) == 3


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("connect"),
        httpx.ReadTimeout("read"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("reset"),
    ],
    ids=["connect-timeout", "read-timeout", "connect-error", "reset"],
)
async def test_a_transport_failure_is_retried_then_transient(
    calls: dict[str, list[tuple[str, str]]], error: Exception
) -> None:
    recorder = Recorder(error)

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder).embed_batch(["x"])

    assert raised.value.failure is EmbeddingFailure.PROVIDER_UNREACHABLE
    assert raised.value.permanent is False
    assert len(recorder.requests) == 3
    assert calls["calls"] == [(EMBED_INGEST, "unavailable")]


# ------------------------------------------------------------ how long to wait


async def test_retry_after_in_seconds_is_honoured() -> None:
    sleeps = Sleeps()
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json=_body(1))
    )

    await _client(recorder, sleeps=sleeps).embed_batch(["x"])

    assert sleeps.waits == [7.0]


async def test_retry_after_as_an_http_date_is_honoured() -> None:
    sleeps = Sleeps()
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=12), usegmt=True)
    recorder = Recorder(
        httpx.Response(503, headers={"retry-after": when}), httpx.Response(200, json=_body(1))
    )

    await _client(recorder, sleeps=sleeps).embed_batch(["x"])

    (wait,) = sleeps.waits
    assert 9.0 <= wait <= 12.0


async def test_a_retry_after_beyond_the_cap_ends_the_retries() -> None:
    sleeps = Sleeps()
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": str(int(MAX_RETRY_AFTER_SECONDS) + 90)})
    )

    with pytest.raises(EmbeddingRateLimitedError):
        await _client(recorder, sleeps=sleeps).embed_batch(["x"])

    assert sleeps.waits == []
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(("jitter", "first", "second"), [(0.0, 0.5, 1.0), (1.0, 1.0, 2.0)])
async def test_without_a_hint_the_backoff_grows_and_is_jittered(
    jitter: float, first: float, second: float
) -> None:
    sleeps = Sleeps()
    recorder = Recorder(httpx.Response(503))

    with pytest.raises(EmbeddingError):
        await _client(recorder, sleeps=sleeps, jitter=jitter).embed_batch(["x"])

    assert sleeps.waits == [first, second]


# ------------------------------------------------------------ secret hygiene


async def test_the_api_key_never_leaves_through_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prose = f"Incorrect API key provided: {SENTINEL_KEY}. You can find your key at ..."
    failure = httpx.Response(
        401, json={"error": {"message": prose, "type": "invalid_request_error", "code": prose}}
    )
    recorder = Recorder(failure)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(EmbeddingError) as raised:
        await _client(recorder, api_key=SENTINEL_KEY).embed_batch(["private document text"])

    # Presence: the sentinel genuinely went out, and genuinely came back.
    assert SENTINEL_KEY in recorder.requests[0].headers["authorization"]
    assert SENTINEL_KEY.encode() in failure.content
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "openai.embeddings_failed" in rendered
    assert SENTINEL_KEY not in rendered
    assert "private document text" not in rendered
    assert SENTINEL_KEY not in str(raised.value)
    assert SENTINEL_KEY not in repr(raised.value.__dict__)
    assert SENTINEL_KEY not in repr(raised.value.args)
