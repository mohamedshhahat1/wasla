"""OpenAI embeddings client.

Spoken over HTTP for the same reasons as the Responses client (ADR-007, ADR-014):
services depend on our own types, and one more endpoint does not justify the
vendor SDK.

The retry policy matches the Responses client rather than the WhatsApp one. A
duplicated embedding request costs tokens; it cannot reach a customer, because
nothing is sent as a result of it. Cost is the only thing at stake, so an
ambiguous failure is retried.
"""

from __future__ import annotations

import asyncio
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

logger = get_logger(__name__)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
EMBEDDINGS_PATH: Final = "/embeddings"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 1.0
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400
# The provider accepts many inputs per call. Batching matters here in a way it
# does not for inference: a document is hundreds of chunks, and one request each
# would turn ingestion into a rate-limit problem.
MAX_BATCH: Final = 96


def build_http_client(*, seconds: float = REQUEST_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """An HTTP client with a bounded timeout, so a stall cannot pin a worker.

    Guarded like every other outbound client (`app.core.net`).
    """
    return build_guarded_client(timeout=httpx.Timeout(seconds))


class EmbeddingsClient:
    """Turns text into vectors.

    The HTTP client, sleep function and attempt budget are injected so retry
    behaviour is testable without a network or a real wait.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        model: str,
        dimensions: int,
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
        self._model = model
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input in order.

        Order is part of the contract: the caller pairs the results back with
        the chunks that produced them, and a reordered response would attach
        each chunk's meaning to its neighbour.
        """
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValidationError("An embedding input cannot be empty.")

        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = texts[start : start + MAX_BATCH]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        """Embed a single text, for the query side of retrieval."""
        vectors = await self.embed([text])
        if not vectors:
            raise ExternalServiceError("The AI provider returned no embedding.")
        return vectors[0]

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            # Requested explicitly so the provider cannot return a width the
            # column will not accept. text-embedding-3-* support truncation.
            "dimensions": self._dimensions,
        }
        body = await self._post(payload)
        return self._vectors(body, expected=len(texts))

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._base_url + EMBEDDINGS_PATH
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        attempt = 1
        while True:
            try:
                response = await self._http.post(url, json=payload, headers=headers)
            except httpx.TransportError as error:
                if attempt >= self._max_attempts:
                    logger.warning("openai.embeddings_unreachable", extra={"attempts": attempt})
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
                        raise RateLimitedError("The AI provider is rate limiting this account.")
                    raise ExternalServiceError("The AI provider is unavailable.")
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code >= CLIENT_ERROR_FLOOR:
                self._log_failure(response, attempts=attempt)
                raise ExternalServiceError("The AI provider rejected the request.")

            return self._decode(response)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_seconds * attempt)

    def _log_failure(self, response: httpx.Response, *, attempts: int) -> None:
        """Log the provider's error code, never its prose.

        Provider error text can echo the request, and a request here contains a
        customer's own documents.
        """
        error: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        logger.warning(
            "openai.embeddings_failed",
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

    def _vectors(self, body: dict[str, Any], *, expected: int) -> list[list[float]]:
        """Pull the vectors out, sorted by the index the provider assigned.

        Sorted rather than trusted in array order: the API documents an `index`
        on each item precisely because the order is not guaranteed, and getting
        this wrong would silently mislabel every chunk.
        """
        data = body.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise ExternalServiceError("The AI provider returned an unexpected embedding count.")

        ordered: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise ExternalServiceError("The AI provider returned an unreadable embedding.")
            raw = item.get("embedding")
            if not isinstance(raw, list) or not raw:
                raise ExternalServiceError("The AI provider returned an unreadable embedding.")
            index = item.get("index")
            ordered.append(
                (
                    index if isinstance(index, int) and not isinstance(index, bool) else position,
                    [float(value) for value in raw],
                )
            )

        ordered.sort(key=lambda pair: pair[0])
        vectors = [vector for _, vector in ordered]
        widths = {len(vector) for vector in vectors}
        if widths != {self._dimensions}:
            # The column is a fixed width; a mismatch has to fail here rather
            # than as a driver error halfway through writing chunks.
            raise ExternalServiceError(
                "The AI provider returned embeddings of an unexpected width."
            )
        return vectors
