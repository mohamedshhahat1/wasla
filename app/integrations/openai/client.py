"""OpenAI Responses API client.

Spoken over HTTP rather than through the vendor SDK: the project already talks
to providers with httpx, services depend on our own types (ADR-007), and one
endpoint does not justify widening the dependency surface.

Retry policy, and why it is the opposite of the WhatsApp client's: a duplicated
inference costs tokens but never reaches a customer, because the orchestrator
decides what to send. An ambiguous retry is therefore a money question here and
a correctness question there.

| Failure | Retried | Reason |
| --- | --- | --- |
| 429 | yes | Rejected outright; nothing was computed |
| transport error | yes | Connect, timeout or protocol; a duplicate is invisible |
| 5xx | yes | Same trade: cost, not customer-visible duplication |
| other 4xx | no | Our request is wrong; repeating it will not help |
| unreadable or oversized 200 | no | The same request would get the same answer |

**How long to wait** (AI-10). A provider that says when to come back in
`Retry-After` - as seconds or as an HTTP date - is believed, up to a cap; a hint
longer than the cap ends the retries rather than being ignored, because
retrying sooner than asked only spends an attempt on a guaranteed refusal.
Without a hint the backoff grows with the attempt and is jittered, so workers
that met one rate limit together do not all retry in the same instant.

**How much to read** (AI-14). The body is streamed and read up to a byte limit.
A success body over it is refused rather than held whole; an error body is only
ever read for its code, so it is truncated instead.

Requests set `store: false` and never use `previous_response_id`. Conversation
memory is assembled from the workspace's own database, so provider-side state
would add retention of customer conversations without adding capability.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.net import build_guarded_client
from app.core.telemetry import CallOutcome, Provider, ProviderCall
from app.integrations.openai.types import (
    AgentReply,
    StructuredFormat,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)

logger = get_logger(__name__)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
RESPONSES_PATH: Final = "/responses"

# What a call is counted under, chosen by the caller from these constants and
# never derived from a prompt or an answer: a metric label domain is chosen where
# it is written, not where a customer types. Split by purpose (AI-09), because
# the classifier runs on every customer message and an agent round only on the
# ones that are answered, and one shared label hid which of them was failing or
# slowing down.
RESPOND_AGENT: Final = "respond_agent"
RESPOND_SENTIMENT: Final = "respond_sentiment"
RESPOND_VISION: Final = "respond_vision"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
# What a request carries when its caller named no output ceiling. The deployment
# default, and deliberately not configurable here: callers are expected to pass
# their own, and this exists only so forgetting is bounded rather than free.
FALLBACK_MAX_OUTPUT_TOKENS: Final = 2_048
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 1.0
# The longest `Retry-After` this client will honour (AI-10). Three attempts of
# sixty seconds already bound a call at three minutes; a provider asking for
# more than half a minute is saying "not soon", and the honest answer is to stop
# and let the turn fail visibly rather than hold a worker for it.
MAX_RETRY_AFTER_SECONDS: Final = 30.0
# The most of one response body this client will read (AI-14). A reply at the
# default output ceiling is a few kilobytes, and a tool-calling reply is smaller;
# a megabyte is generous by two orders of magnitude and still refuses the
# multi-megabyte body a misbehaving provider or proxy could otherwise make every
# worker hold in memory at once.
MAX_RESPONSE_BYTES: Final = 1_048_576
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400


class _ResponseTooLargeError(Exception):
    """A success body past the byte limit. Internal; surfaced as a refusal."""


def _attempt_outcome(status: int) -> CallOutcome:
    """The closed outcome domain for one non-success HTTP answer."""
    if status == TOO_MANY_REQUESTS:
        return CallOutcome.RATE_LIMITED
    if status >= SERVER_ERROR_FLOOR:
        return CallOutcome.UNAVAILABLE
    if status >= CLIENT_ERROR_FLOOR:
        return CallOutcome.FAILURE
    return CallOutcome.SUCCESS


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """How long a `Retry-After` header asks the client to wait, or None.

    Both forms RFC 9110 allows: a number of seconds, and an HTTP date. Anything
    else - negative, not finite, unparseable - is treated as no hint at all
    rather than guessed at, and a date already past means "now".
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max((moment - (now or datetime.now(UTC))).total_seconds(), 0.0)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


async def _read_bounded(response: httpx.Response, *, limit: int, refuse: bool) -> bytes:
    """Read at most `limit` bytes of a streamed body.

    `refuse` raises past the limit - for a success body, which is only useful
    whole. Otherwise the body is cut at the limit - for an error body, which is
    only ever read for its code.
    """
    declared = response.headers.get("content-length", "")
    if refuse and declared.isdigit() and int(declared) > limit:
        raise _ResponseTooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        if size + len(chunk) > limit:
            if refuse:
                raise _ResponseTooLargeError
            chunks.append(chunk[: limit - size])
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def build_http_client(*, seconds: float = REQUEST_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """An HTTP client with a bounded timeout, aimed only at public addresses.

    Inference is slow enough that the default is generous, but never absent: a
    provider stall must not pin a worker indefinitely.

    The guarded transport is not needed here in the sense that every URL this
    client builds comes from a constant - and it is used anyway, so that the
    answer to "which clients are guarded?" is "all of them" rather than a list
    that goes stale the first time somebody adds an integration.
    """
    return build_guarded_client(timeout=httpx.Timeout(seconds))


class ResponsesClient:
    """Calls the Responses API and returns our own reply type.

    The HTTP client, sleep function, jitter source and attempt budget are
    injected so retry behaviour is testable without a network or a real wait.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not api_key:
            # Our misconfiguration, not the caller's mistake.
            raise DependencyUnavailableError("The OpenAI API key is not configured.")
        self._http = http
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._max_response_bytes = max(1, max_response_bytes)

    async def respond(
        self,
        *,
        model: str,
        instructions: str,
        turns: Sequence[Turn],
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolResult] = (),
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        response_format: StructuredFormat | None = None,
        operation: str = RESPOND_AGENT,
    ) -> AgentReply:
        """Run one inference.

        `tool_results` continues an exchange the model started: pass the results
        of the calls it asked for and it will either answer or ask for more.

        `response_format` constrains the reply to a JSON shape, for callers that
        parse it rather than send it. The reply still arrives as text; decoding
        it belongs to the caller, which is the only thing that knows what the
        shape means.
        """
        if not turns and not tool_results:
            raise ValidationError("An agent call needs at least one input item.")

        items: list[dict[str, Any]] = [turn.to_input() for turn in turns]
        for result in tool_results:
            items.extend(result.to_input())

        payload: dict[str, Any] = {
            "model": model,
            "input": items,
            # Never retain customer conversations provider-side.
            "store": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [spec.to_payload() for spec in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        # Never absent (AI-05). A request without a ceiling buys whatever output
        # the provider's own default allows, which is a per-call spend nobody
        # chose; every caller passes one, and this is what holds if one forgets.
        payload["max_output_tokens"] = (
            max_output_tokens if max_output_tokens is not None else FALLBACK_MAX_OUTPUT_TOKENS
        )
        if response_format is not None:
            payload["text"] = {"format": response_format.to_payload()}

        body = await self._post(payload, operation=operation)
        return self._reply(body)

    async def _post(self, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        # One inference, observed here rather than in the worker because this
        # is where the outcome is already distinguished: a 429, a 5xx and a
        # refused request are three different operational problems and only
        # this loop can tell them apart. Token *spend* is not counted here - it
        # is already metered into `usage_events`, and a second tally would be a
        # second number to reconcile.
        #
        # The clock starts here rather than around the HTTP call below, so the
        # duration covers the retries too: what the agent turn waited on is
        # this whole method, and a call that succeeded on its third attempt was
        # slow for the customer however fast the third attempt was.
        call = ProviderCall(provider=Provider.OPENAI, operation=operation)
        url = self._base_url + RESPONSES_PATH
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 1
        while True:
            try:
                async with self._http.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    status = response.status_code
                    hint = retry_after_seconds(response.headers.get("retry-after"))
                    content = await _read_bounded(
                        response,
                        limit=self._max_response_bytes,
                        refuse=status < CLIENT_ERROR_FLOOR,
                    )
            except httpx.TransportError as error:
                # Connect, timeout and protocol failures alike: retrying can at
                # worst duplicate an inference, which no customer ever sees.
                await call.attempt(CallOutcome.UNAVAILABLE)
                if attempt >= self._max_attempts:
                    logger.warning("openai.unreachable", extra={"attempts": attempt})
                    await call.record(CallOutcome.UNAVAILABLE)
                    raise ExternalServiceError("The AI provider could not be reached.") from error
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            except _ResponseTooLargeError:
                # Refused, not retried: the same request would produce the same
                # body. Never read whole, never logged.
                await call.attempt(CallOutcome.FAILURE)
                logger.warning(
                    "openai.response_too_large",
                    extra={"attempts": attempt, "limit_bytes": self._max_response_bytes},
                )
                await call.record(CallOutcome.FAILURE)
                raise ExternalServiceError(
                    "The AI provider returned a response too large to read."
                ) from None

            if status < CLIENT_ERROR_FLOOR:
                try:
                    body = self._decode(content)
                except ExternalServiceError:
                    await call.attempt(CallOutcome.FAILURE)
                    await call.record(CallOutcome.FAILURE)
                    raise
                # Every attempt, not only the last (AI-09): a call that succeeds
                # on its third try is a success once and two throttled attempts.
                await call.attempt(CallOutcome.SUCCESS)
                await call.record(CallOutcome.SUCCESS)
                return body

            await call.attempt(_attempt_outcome(status))
            retryable = status == TOO_MANY_REQUESTS or status >= SERVER_ERROR_FLOOR
            if not retryable:
                self._log_failure(status, content, attempts=attempt)
                await call.record(CallOutcome.FAILURE)
                raise ExternalServiceError("The AI provider rejected the request.")

            delay = self._delay(attempt, hint)
            if attempt >= self._max_attempts or delay is None:
                self._log_failure(status, content, attempts=attempt, retry_after=hint)
                if status == TOO_MANY_REQUESTS:
                    await call.record(CallOutcome.RATE_LIMITED)
                    raise RateLimitedError("The AI provider is rate limiting this account.")
                await call.record(CallOutcome.UNAVAILABLE)
                raise ExternalServiceError("The AI provider is unavailable.")
            await self._sleep(delay)
            attempt += 1

    def _delay(self, attempt: int, hint: float | None) -> float | None:
        """How long to wait before the next attempt, or None to stop retrying.

        A provider hint within the cap is honoured as given. One beyond it ends
        the retries: waiting less than asked spends an attempt on a refusal, and
        waiting as long as asked holds a worker for it.
        """
        if hint is not None:
            return hint if hint <= MAX_RETRY_AFTER_SECONDS else None
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        """Linear backoff with equal jitter: half fixed, half random.

        The fixed half keeps a floor under the wait, so a retry is never
        immediate; the random half spreads workers that failed together, which
        is the synchronised burst an unjittered schedule turns a brief throttle
        into (AI-10).
        """
        computed = self._backoff_seconds * attempt
        return computed / 2 + min(max(self._jitter(), 0.0), 1.0) * computed / 2

    def _log_failure(
        self,
        status: int,
        content: bytes,
        *,
        attempts: int,
        retry_after: float | None = None,
    ) -> None:
        """Log the provider's error code, never its prose.

        Provider error text can echo the request, and a request here contains a
        customer conversation - and the provider's own message for a bad key
        quotes the key back. So only fields from a closed vocabulary leave.
        """
        error: dict[str, Any] = {}
        try:
            body = json.loads(content) if content else {}
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        logger.warning(
            "openai.request_failed",
            extra={
                "status": status,
                "attempts": attempts,
                "provider_code": error.get("code"),
                "provider_type": error.get("type"),
                "retry_after_seconds": retry_after,
            },
        )

    def _decode(self, content: bytes) -> dict[str, Any]:
        try:
            body = json.loads(content)
        except ValueError as error:
            raise ExternalServiceError(
                "The AI provider returned an unreadable response."
            ) from error
        if not isinstance(body, dict):
            raise ExternalServiceError("The AI provider returned an unexpected response.")
        return body

    def _reply(self, body: dict[str, Any]) -> AgentReply:
        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for item in self._items(body):
            item_type = item.get("type")
            if item_type == "function_call":
                call = self._tool_call(item)
                if call is not None:
                    calls.append(call)
            elif item_type == "message":
                text_parts.append(self._message_text(item))

        text = "".join(text_parts).strip()
        response_id = body.get("id")
        usage = body.get("usage")
        return AgentReply(
            text=text or None,
            tool_calls=tuple(calls),
            usage=TokenUsage.from_payload(usage),
            response_id=response_id if isinstance(response_id, str) else None,
            usage_payload=usage if isinstance(usage, dict) else None,
        )

    def _items(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        output = body.get("output")
        if not isinstance(output, list):
            return []
        return [item for item in output if isinstance(item, dict)]

    def _message_text(self, item: dict[str, Any]) -> str:
        content = item.get("content")
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    def _tool_call(self, item: dict[str, Any]) -> ToolCall | None:
        """Decode one requested call.

        Unparseable arguments are reduced to an empty object rather than raised:
        a model calling a tool wrongly is an ordinary event, and the registry can
        reject it with a message the model can act on. Failing the whole reply
        would discard any text it produced as well.
        """
        call_id = item.get("call_id")
        name = item.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            logger.warning("openai.tool_call_unidentifiable")
            return None

        raw_arguments = item.get("arguments")
        arguments_json = raw_arguments if isinstance(raw_arguments, str) else "{}"
        arguments: dict[str, Any] = {}
        try:
            decoded = json.loads(arguments_json)
        except ValueError:
            logger.warning("openai.tool_arguments_invalid", extra={"tool": name})
        else:
            if isinstance(decoded, dict):
                arguments = decoded
            else:
                logger.warning("openai.tool_arguments_not_an_object", extra={"tool": name})

        return ToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
            arguments_json=arguments_json,
        )
