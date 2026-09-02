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

Requests set `store: false` and never use `previous_response_id`. Conversation
memory is assembled from the workspace's own database, so provider-side state
would add retention of customer conversations without adding capability.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
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
from app.core.telemetry import CallOutcome, Provider, record_provider_call
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

# What this client is counted under. A fixed constant, never anything
# derived from the prompt or the model's answer: a metric label domain is
# chosen where it is written, not where a customer types.
RESPOND: Final = "respond"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 1.0
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400


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

    The HTTP client, sleep function and attempt budget are injected so retry
    behaviour is testable without a network or a real wait.
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
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        if response_format is not None:
            payload["text"] = {"format": response_format.to_payload()}

        body = await self._post(payload)
        return self._reply(body)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._base_url + RESPONSES_PATH
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 1
        while True:
            try:
                response = await self._http.post(url, json=payload, headers=headers)
            except httpx.TransportError as error:
                # Connect, timeout and protocol failures alike: retrying can at
                # worst duplicate an inference, which no customer ever sees.
                if attempt >= self._max_attempts:
                    logger.warning("openai.unreachable", extra={"attempts": attempt})
                    await _count(CallOutcome.UNAVAILABLE)
                    raise ExternalServiceError("The AI provider could not be reached.") from error
                await self._backoff(attempt)
                attempt += 1
                continue

            retryable = (
                response.status_code == TOO_MANY_REQUESTS
                or response.status_code >= SERVER_ERROR_FLOOR
            )
            if retryable:
                if attempt >= self._max_attempts:
                    self._log_failure(response, attempts=attempt)
                    if response.status_code == TOO_MANY_REQUESTS:
                        await _count(CallOutcome.RATE_LIMITED)
                        raise RateLimitedError("The AI provider is rate limiting this account.")
                    await _count(CallOutcome.UNAVAILABLE)
                    raise ExternalServiceError("The AI provider is unavailable.")
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code >= CLIENT_ERROR_FLOOR:
                self._log_failure(response, attempts=attempt)
                await _count(CallOutcome.FAILURE)
                raise ExternalServiceError("The AI provider rejected the request.")

            await _count(CallOutcome.SUCCESS)
            return self._decode(response)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_seconds * attempt)

    def _log_failure(self, response: httpx.Response, *, attempts: int) -> None:
        """Log the provider's error code, never its prose.

        Provider error text can echo the request, and a request here contains a
        customer conversation.
        """
        error: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        logger.warning(
            "openai.request_failed",
            extra={
                "status": response.status_code,
                "attempts": attempts,
                "provider_code": error.get("code"),
                "provider_type": error.get("type"),
            },
        )

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
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
        return AgentReply(
            text=text or None,
            tool_calls=tuple(calls),
            usage=TokenUsage.from_payload(body.get("usage")),
            response_id=response_id if isinstance(response_id, str) else None,
            raw=body,
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


async def _count(outcome: CallOutcome) -> None:
    """Record one inference attempt's outcome. Best-effort by construction.

    Counted here rather than in the worker because this is where the outcome
    is already distinguished: a 429, a 5xx and a refused request are three
    different operational problems and only this loop can tell them apart.
    Token *spend* is not counted here - it is already metered into
    `usage_events`, and a second tally would be a second number to reconcile.
    """
    await record_provider_call(provider=Provider.OPENAI, operation=RESPOND, outcome=outcome)
