"""The Responses API client's contract, through real httpx serialization (AI-13).

`tests/real_provider/` proves the provider really accepts this shape - and skips
whenever there is no credential, which is every CI run. So until this file, no
credential-free test pinned what the client itself does: three meaningful
mutations survived the repository's suite - a malformed HTTP 200 treated as
usable, a 401 made retryable, and the provider's error prose logged with the API
key quoted inside it.

Everything here runs the real client over `httpx.MockTransport`, so request
bytes, headers, streaming, status handling and JSON decoding are all the real
ones. Every assertion about what did *not* happen is paired with a count of what
did.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import JsonFormatter
from app.integrations.openai.client import (
    FALLBACK_MAX_OUTPUT_TOKENS,
    MAX_RETRY_AFTER_SECONDS,
    OPENAI_BASE_URL,
    RESPONSES_PATH,
    ResponsesClient,
    retry_after_seconds,
)
from app.integrations.openai.types import (
    MAX_REPORTED_TOKENS,
    StructuredFormat,
    TokenUsage,
    ToolSpec,
    Turn,
)

SENTINEL_KEY = "sk-AUDIT-SENTINEL-0123456789"
HELLO = [Turn(role="user", text="hello")]


def _ok(text: str | None = "Hello there.", **extra: Any) -> httpx.Response:
    output: list[dict[str, Any]] = []
    if text is not None:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    body = {
        "id": "resp_contract",
        "output": output,
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }
    body.update(extra)
    return httpx.Response(200, json=body)


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
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


def _client(
    recorder: Recorder,
    *,
    sleeps: Sleeps | None = None,
    jitter: Callable[[], float] = lambda: 0.5,
    max_response_bytes: int | None = None,
    api_key: str = "sk-test-not-real",
) -> ResponsesClient:
    extra: dict[str, Any] = {}
    if max_response_bytes is not None:
        extra["max_response_bytes"] = max_response_bytes
    return ResponsesClient(
        http=httpx.AsyncClient(transport=recorder.transport()),
        api_key=api_key,
        sleep=sleeps if sleeps is not None else Sleeps(),
        jitter=jitter,
        **extra,
    )


# ------------------------------------------------------------ request shape


async def test_a_request_goes_to_the_responses_endpoint_with_a_bearer_key() -> None:
    recorder = Recorder(_ok())

    await _client(recorder).respond(model="gpt-4.1-mini", instructions="Be brief.", turns=HELLO)

    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == OPENAI_BASE_URL + RESPONSES_PATH
    assert request.headers["authorization"] == "Bearer sk-test-not-real"
    assert request.headers["content-type"] == "application/json"


async def test_the_request_body_has_the_shape_the_provider_expects() -> None:
    recorder = Recorder(_ok())
    tool = ToolSpec(
        name="search_knowledge",
        description="Search.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )

    await _client(recorder).respond(
        model="gpt-4.1-mini",
        instructions="Be brief.",
        turns=[Turn(role="user", text="hi"), Turn(role="assistant", text="hello")],
        tools=[tool],
        temperature=0.3,
        max_output_tokens=512,
    )

    payload = recorder.payload()
    assert payload == {
        "model": "gpt-4.1-mini",
        "input": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "store": False,
        "instructions": "Be brief.",
        "tools": [
            {
                "type": "function",
                "name": "search_knowledge",
                "description": "Search.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
        "temperature": 0.3,
        "max_output_tokens": 512,
    }
    assert "previous_response_id" not in payload


async def test_a_request_without_a_ceiling_still_carries_one() -> None:
    recorder = Recorder(_ok())

    await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert recorder.payload()["max_output_tokens"] == FALLBACK_MAX_OUTPUT_TOKENS


async def test_a_structured_format_is_sent_strict() -> None:
    recorder = Recorder(_ok('{"a": 1}'))
    shape = StructuredFormat(name="reading", schema={"type": "object"})

    await _client(recorder).respond(model="m", instructions="", turns=HELLO, response_format=shape)

    assert recorder.payload()["text"] == {
        "format": {
            "type": "json_schema",
            "name": "reading",
            "schema": {"type": "object"},
            "strict": True,
        }
    }


def test_a_missing_key_is_our_misconfiguration() -> None:
    with pytest.raises(DependencyUnavailableError):
        ResponsesClient(http=httpx.AsyncClient(), api_key="")


async def test_a_call_with_nothing_to_say_makes_no_request() -> None:
    recorder = Recorder(_ok())

    with pytest.raises(ValidationError):
        await _client(recorder).respond(model="m", instructions="", turns=[])

    assert recorder.requests == []


# --------------------------------------------------------- reading the answer


async def test_text_usage_and_response_id_are_read() -> None:
    reply = await _client(Recorder(_ok("Hi!"))).respond(model="m", instructions="", turns=HELLO)

    assert reply.text == "Hi!"
    assert reply.usage == TokenUsage(input_tokens=12, output_tokens=3, total_tokens=15)
    assert reply.response_id == "resp_contract"
    assert reply.usage_payload == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b""),
        httpx.Response(200, content=b"{not json"),
        httpx.Response(200, content=b'{"output": ['),
        httpx.Response(200, json=[1, 2, 3]),
        httpx.Response(200, json="a string"),
        httpx.Response(200, content=b"<html>ok</html>", headers={"content-type": "text/html"}),
    ],
    ids=["empty", "invalid", "truncated", "list", "string", "html"],
)
async def test_an_unreadable_success_body_is_refused_after_one_attempt(
    response: httpx.Response,
) -> None:
    """A malformed HTTP 200 never becomes a reply (was mutation AI-M08)."""
    recorder = Recorder(response)

    with pytest.raises(ExternalServiceError):
        await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        [{"type": "message", "content": "not a list"}],
        [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
        [{"type": "reasoning"}],
    ],
    ids=["null", "empty", "string-content", "refusal", "unknown-item"],
)
async def test_a_well_formed_answer_with_no_words_has_no_text(output: object) -> None:
    reply = await _client(Recorder(_ok(None, output=output))).respond(
        model="m", instructions="", turns=HELLO
    )

    assert reply.text is None
    assert reply.tool_calls == ()


async def test_unparseable_tool_arguments_become_an_empty_object() -> None:
    body = _ok(
        None,
        output=[
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{nope"},
            {"type": "function_call", "name": "no_id", "arguments": "{}"},
        ],
    )

    reply = await _client(Recorder(body)).respond(model="m", instructions="", turns=HELLO)

    (call,) = reply.tool_calls
    assert (call.name, call.arguments, call.arguments_json) == ("lookup", {}, "{nope")


# -------------------------------------------------------------- status matrix


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_client_error_is_not_retried(status: int) -> None:
    """Retrying a refused key forever is a cost and rate-limit hazard (was AI-M09)."""
    recorder = Recorder(httpx.Response(status, json={"error": {"code": "nope"}}))
    sleeps = Sleeps()

    with pytest.raises(ExternalServiceError):
        await _client(recorder, sleeps=sleeps).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 1
    assert sleeps.waited == []


async def test_rate_limiting_is_retried_then_reported() -> None:
    recorder = Recorder(httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}}))

    with pytest.raises(RateLimitedError):
        await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 3


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_server_error_is_retried_then_reported(status: int) -> None:
    recorder = Recorder(httpx.Response(status))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 3


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connect", "read-timeout"],
)
async def test_a_transport_failure_is_retried_then_reported(failure: Exception) -> None:
    recorder = Recorder(failure)

    with pytest.raises(ExternalServiceError):
        await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 3


async def test_a_retry_that_succeeds_returns_the_reply() -> None:
    recorder = Recorder(httpx.Response(503), _ok("Recovered."))

    reply = await _client(recorder).respond(model="m", instructions="", turns=HELLO)

    assert reply.text == "Recovered."
    assert len(recorder.requests) == 2


# ------------------------------------------------------- Retry-After (AI-10)


async def test_retry_after_in_seconds_is_honoured() -> None:
    recorder = Recorder(httpx.Response(429, headers={"retry-after": "7"}), _ok())
    sleeps = Sleeps()

    await _client(recorder, sleeps=sleeps).respond(model="m", instructions="", turns=HELLO)

    assert sleeps.waited == [7.0]
    assert len(recorder.requests) == 2


async def test_retry_after_as_an_http_date_is_honoured() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=12), usegmt=True)
    recorder = Recorder(httpx.Response(503, headers={"retry-after": when}), _ok())
    sleeps = Sleeps()

    await _client(recorder, sleeps=sleeps).respond(model="m", instructions="", turns=HELLO)

    (waited,) = sleeps.waited
    assert 9.0 <= waited <= 12.0


async def test_a_hint_longer_than_the_cap_ends_the_retries() -> None:
    """Waiting less than asked spends an attempt on a guaranteed refusal."""
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": str(MAX_RETRY_AFTER_SECONDS + 90)})
    )
    sleeps = Sleeps()

    with pytest.raises(RateLimitedError):
        await _client(recorder, sleeps=sleeps).respond(model="m", instructions="", turns=HELLO)

    assert len(recorder.requests) == 1
    assert sleeps.waited == []


@pytest.mark.parametrize("header", ["soon", "-5", "inf", "nan", ""])
def test_an_unusable_hint_is_no_hint(header: str) -> None:
    assert retry_after_seconds(header) is None


def test_a_date_already_past_means_now() -> None:
    past = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)

    assert retry_after_seconds(past) == 0.0


# ------------------------------------------------------------- jitter (AI-10)


async def test_backoff_is_half_fixed_and_half_jittered() -> None:
    recorder = Recorder(httpx.Response(503))

    lowest, highest = Sleeps(), Sleeps()
    with pytest.raises(ExternalServiceError):
        await _client(recorder, sleeps=lowest, jitter=lambda: 0.0).respond(
            model="m", instructions="", turns=HELLO
        )
    with pytest.raises(ExternalServiceError):
        await _client(recorder, sleeps=highest, jitter=lambda: 1.0).respond(
            model="m", instructions="", turns=HELLO
        )

    assert lowest.waited == [0.5, 1.0]
    assert highest.waited == [1.0, 2.0]


async def test_workers_meeting_one_rate_limit_do_not_retry_in_lockstep() -> None:
    """With the real jitter source, twenty clients do not all choose one wait."""
    firsts: list[float] = []
    for _ in range(20):
        sleeps = Sleeps()
        client = ResponsesClient(
            http=httpx.AsyncClient(transport=Recorder(httpx.Response(503), _ok()).transport()),
            api_key="sk-test-not-real",
            sleep=sleeps,
        )
        await client.respond(model="m", instructions="", turns=HELLO)
        firsts.append(sleeps.waited[0])

    assert all(0.5 <= wait <= 1.0 for wait in firsts)
    assert len(set(firsts)) > 1


# ---------------------------------------------------- usage validation (AI-11)


@pytest.mark.parametrize(
    "value",
    [-5, True, 5.5, "10", 10**30, MAX_REPORTED_TOKENS + 1],
    ids=["negative", "boolean", "float", "string", "absurd", "past-the-ceiling"],
)
def test_an_unusable_token_count_becomes_zero(value: object) -> None:
    usage = TokenUsage.from_payload(
        {"input_tokens": value, "output_tokens": 4, "total_tokens": value}
    )

    assert usage == TokenUsage(input_tokens=0, output_tokens=4, total_tokens=0)


def test_a_plausible_count_is_kept_exactly() -> None:
    usage = TokenUsage.from_payload(
        {"input_tokens": MAX_REPORTED_TOKENS, "output_tokens": 0, "total_tokens": 7}
    )

    assert usage == TokenUsage(input_tokens=MAX_REPORTED_TOKENS, output_tokens=0, total_tokens=7)


@pytest.mark.parametrize("payload", [None, {}, [], "usage"], ids=["none", "empty", "list", "str"])
def test_a_missing_usage_object_is_zero(payload: object) -> None:
    assert TokenUsage.from_payload(payload) == TokenUsage(0, 0, 0)


# ------------------------------------------------------- size bounds (AI-14)


class _CountingStream(httpx.AsyncByteStream):
    """A body of unknown length, counting how much of it was actually pulled."""

    def __init__(self, *, chunk: bytes, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.pulled += len(self._chunk)
            yield self._chunk


async def test_a_declared_oversized_body_is_refused_without_being_read() -> None:
    body = json.dumps({"output": [], "padding": "x" * 2_000_000}).encode()
    recorder = Recorder(httpx.Response(200, content=body))

    with pytest.raises(ExternalServiceError):
        await _client(recorder, max_response_bytes=1_048_576).respond(
            model="m", instructions="", turns=HELLO
        )

    assert len(recorder.requests) == 1


async def test_a_streamed_oversized_body_is_abandoned_at_the_limit() -> None:
    stream = _CountingStream(chunk=b"x" * 65_536, chunks=40)  # 2.6 MB on offer
    recorder = Recorder(httpx.Response(200, stream=stream))

    with pytest.raises(ExternalServiceError):
        await _client(recorder, max_response_bytes=1_048_576).respond(
            model="m", instructions="", turns=HELLO
        )

    assert 0 < stream.pulled <= 1_048_576 + 65_536, "reading stopped at the limit"
    assert len(recorder.requests) == 1


async def test_an_error_body_is_truncated_rather_than_refused() -> None:
    """An oversized 503 is still a 503, and still retried."""
    stream = _CountingStream(chunk=b"<html>" + b"x" * 65_530, chunks=40)
    recorder = Recorder(httpx.Response(503, stream=stream), _ok("Back."))

    reply = await _client(recorder, max_response_bytes=1_048_576).respond(
        model="m", instructions="", turns=HELLO
    )

    assert reply.text == "Back."
    assert len(recorder.requests) == 2


# --------------------------------------------------------- secret hygiene


async def test_the_api_key_never_appears_in_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The provider's own 401 message quotes the key back (was mutation AI-M14b).

    Rendered through the real `JsonFormatter`, so log extras are inspected as they
    would be written - an assertion on `record.getMessage()` alone would miss a
    field added under a harmless name.
    """
    prose = f"Incorrect API key provided: {SENTINEL_KEY}. You can find your key at ..."
    failure = httpx.Response(
        401,
        json={"error": {"message": prose, "type": "invalid_request_error", "code": "bad_key"}},
    )
    recorder = Recorder(failure)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ExternalServiceError) as raised:
        await _client(recorder, api_key=SENTINEL_KEY).respond(
            model="m", instructions="", turns=HELLO
        )

    # Non-vacuity: the key really was on the wire and really was in the body.
    assert SENTINEL_KEY in recorder.requests[0].headers["authorization"]
    assert SENTINEL_KEY.encode() in failure.content
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "openai.request_failed" in rendered, "the failure was genuinely logged"
    assert SENTINEL_KEY not in rendered
    assert "Incorrect API key" not in rendered
    assert SENTINEL_KEY not in str(raised.value)
    assert SENTINEL_KEY not in repr(raised.value.__dict__)
    assert SENTINEL_KEY not in repr(raised.value.args)
