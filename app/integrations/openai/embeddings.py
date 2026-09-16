"""OpenAI embeddings client.

Spoken over HTTP for the same reasons as the Responses client (ADR-007, ADR-014):
services depend on our own types, and one more endpoint does not justify the
vendor SDK.

It is held to the same operational standard as that client (RAG-16), because it
fails in the same ways and was found failing in all of them:

| Failure | Retried here | Outcome for the caller |
| --- | --- | --- |
| 429 | yes, honouring `Retry-After` up to a cap | `EmbeddingRateLimitedError`, transient |
| transport error | yes | transient |
| 5xx | yes | transient |
| 400 / 401 / 403 / 404 / 422 | **no** | permanent: the same request is refused again |
| body over the byte limit | no | permanent: the same request returns the same body |
| unreadable 200 | no | transient: a proxy's truncated page is not the model's answer |
| a vector that is not exact-width, finite, typed and non-zero | no | permanent |

**Permanent and transient are part of the error, not a guess made later.** The
ingestion worker decides from `EmbeddingError.permanent` whether a document's
retry budget is worth spending at all (RAG-01). Before this, a revoked key was an
`ExternalServiceError` like a 503 was, and a document built on it was re-embedded
every minute for ever.

**A vector is validated element by element before anything else sees it**
(RAG-11, RAG-03). Python's JSON parser admits `NaN` and `Infinity`; `float()`
turns `true` into 1.0 and `"0.5"` into 0.5; a vector of zeros has no direction
and an undefined cosine distance. pgvector either refuses those - as a
`DataError` in the middle of somebody's agent turn - or stores them, and a stored
zero vector is a passage that is `ready` and can never be found.

**How much is read is bounded by what was asked** (RAG-16). A body is streamed
and read up to a limit sized from the request - inputs times width times a
generous per-number allowance - so a one-line query cannot be answered with a
megabyte and a full batch is not refused for being the size it has to be.

**Every attempt is counted** under `embed_ingest` or `embed_query` (RAG-05), so
a rejected key or a throttled account shows up as embeddings failing rather than
as every agent quietly having nothing to say.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.net import build_guarded_client
from app.core.telemetry import CallOutcome, Provider, ProviderCall

# The Responses client's primitives, reused rather than copied: one parser for
# `Retry-After` and one bounded reader means one place each can be wrong.
from app.integrations.openai.client import (
    _attempt_outcome,
    _read_bounded,
    _ResponseTooLargeError,
    retry_after_seconds,
)

logger = get_logger(__name__)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
EMBEDDINGS_PATH: Final = "/embeddings"
REQUEST_TIMEOUT_SECONDS: Final = 60.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 1.0
# The longest `Retry-After` honoured. Matches the Responses client: a provider
# asking for more than half a minute is saying "not soon", and the answer that
# holds a worker least is to stop and let the caller's own budget decide.
MAX_RETRY_AFTER_SECONDS: Final = 30.0
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400
# The provider accepts many inputs per call. Batching matters here in a way it
# does not for inference: a document is hundreds of chunks, and one request each
# would turn ingestion into a rate-limit problem.
MAX_BATCH: Final = 96

# How a call is counted. Chosen by whoever builds the client, never derived from
# a request (see `ProviderCall`).
EMBED_INGEST: Final = "embed_ingest"
EMBED_QUERY: Final = "embed_query"

# The response budget per requested number. A float in the provider's JSON is
# about twenty characters with its separator; thirty-two leaves room for a
# pretty-printing proxy without leaving room for a body nobody asked for.
RESPONSE_BYTES_PER_VALUE: Final = 32
# Envelope, `usage`, `model`, and the per-item keys.
RESPONSE_OVERHEAD_BYTES: Final = 65_536
# An error body is only ever read for its code.
MAX_ERROR_BODY_BYTES: Final = 65_536
# Below this a vector has no usable direction. OpenAI's are unit length, so a
# real vector's squared norm is about 1 and this only catches the degenerate.
MIN_SQUARED_NORM: Final = 1e-12
# A token count past this is not a count; it is a broken payload.
MAX_REPORTED_INPUT_TOKENS: Final = 100_000_000


class EmbeddingFailure(StrEnum):
    """Why an embedding call failed, in a closed vocabulary.

    Persisted as a document's `last_error_code` and used to decide whether a
    retry is worth its cost, which is why it is an enum and never the provider's
    own prose: that prose is unbounded and, for a bad key, quotes the key.
    """

    PROVIDER_INVALID_REQUEST = "provider_invalid_request"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    PROVIDER_FORBIDDEN = "provider_forbidden"
    PROVIDER_MODEL_NOT_FOUND = "provider_model_not_found"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    RESPONSE_TOO_LARGE = "provider_response_too_large"
    UNREADABLE_RESPONSE = "provider_unreadable_response"
    INVALID_EMBEDDING = "invalid_embedding"


# Failures the same request will meet again. Retrying them spends money on a
# guaranteed refusal; the only thing that changes their outcome is a person
# fixing a key, a model name or the provider's output.
PERMANENT_FAILURES: Final[frozenset[EmbeddingFailure]] = frozenset(
    {
        EmbeddingFailure.PROVIDER_INVALID_REQUEST,
        EmbeddingFailure.PROVIDER_UNAUTHORIZED,
        EmbeddingFailure.PROVIDER_FORBIDDEN,
        EmbeddingFailure.PROVIDER_MODEL_NOT_FOUND,
        EmbeddingFailure.RESPONSE_TOO_LARGE,
        EmbeddingFailure.INVALID_EMBEDDING,
    }
)

_MESSAGES: Final[dict[EmbeddingFailure, str]] = {
    EmbeddingFailure.PROVIDER_INVALID_REQUEST: "The AI provider rejected the request.",
    EmbeddingFailure.PROVIDER_UNAUTHORIZED: "The AI provider rejected the request.",
    EmbeddingFailure.PROVIDER_FORBIDDEN: "The AI provider rejected the request.",
    EmbeddingFailure.PROVIDER_MODEL_NOT_FOUND: "The AI provider rejected the request.",
    EmbeddingFailure.PROVIDER_RATE_LIMITED: "The AI provider is rate limiting this account.",
    EmbeddingFailure.PROVIDER_UNAVAILABLE: "The AI provider is unavailable.",
    EmbeddingFailure.PROVIDER_UNREACHABLE: "The AI provider could not be reached.",
    EmbeddingFailure.RESPONSE_TOO_LARGE: "The AI provider returned a response too large to read.",
    EmbeddingFailure.UNREADABLE_RESPONSE: "The AI provider returned an unreadable response.",
    EmbeddingFailure.INVALID_EMBEDDING: "The AI provider returned an invalid embedding.",
}

_CLIENT_FAILURES: Final[dict[int, EmbeddingFailure]] = {
    401: EmbeddingFailure.PROVIDER_UNAUTHORIZED,
    403: EmbeddingFailure.PROVIDER_FORBIDDEN,
    404: EmbeddingFailure.PROVIDER_MODEL_NOT_FOUND,
}


class EmbeddingError(ExternalServiceError):
    """An embedding call that failed, and whether trying again could help."""

    def __init__(self, failure: EmbeddingFailure, message: str | None = None) -> None:
        super().__init__(message or _MESSAGES[failure])
        self.failure = failure

    @property
    def permanent(self) -> bool:
        return self.failure in PERMANENT_FAILURES


class EmbeddingRateLimitedError(RateLimitedError):
    """429 after the retries, kept a `RateLimitedError` so workers classify it so."""

    failure = EmbeddingFailure.PROVIDER_RATE_LIMITED
    permanent = False

    def __init__(self) -> None:
        super().__init__(_MESSAGES[EmbeddingFailure.PROVIDER_RATE_LIMITED])


def embedding_failure(error: BaseException) -> EmbeddingFailure | None:
    """The failure an exception carries, if it came from this client."""
    if isinstance(error, EmbeddingError | EmbeddingRateLimitedError):
        return error.failure
    return None


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """One provider call's vectors, and what it reported having consumed.

    `input_tokens` is None when the provider did not say, which is a real
    answer: a usage row is then recorded in characters rather than invented in
    tokens.
    """

    vectors: list[list[float]]
    input_tokens: int | None
    characters: int


def validate_vector(raw: object, *, dimensions: int) -> list[float]:
    """A provider vector as floats, or `EmbeddingError(INVALID_EMBEDDING)`.

    Strict on purpose. Each element must *be* a JSON number - a `bool` is an
    `int` to Python and is refused, a numeric string is refused - must be
    finite, and the vector must have exactly the requested width and a direction.
    Coercion is what let `true` and `"0.5"` become stored, "ready" vectors.
    """
    if not isinstance(raw, list) or len(raw) != dimensions:
        raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
    vector: list[float] = []
    squared = 0.0
    for value in raw:
        # `type(...) in` rather than `isinstance`, because `bool` subclasses
        # `int` and `True` must not pass as 1.
        if type(value) not in (float, int):
            raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
        vector.append(number)
        squared += number * number
    if not math.isfinite(squared) or squared < MIN_SQUARED_NORM:
        raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
    return vector


def build_http_client(*, seconds: float = REQUEST_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """An HTTP client with a bounded timeout, so a stall cannot pin a worker.

    Guarded like every other outbound client (`app.core.net`).
    """
    return build_guarded_client(timeout=httpx.Timeout(seconds))


class EmbeddingsClient:
    """Turns text into vectors.

    The HTTP client, sleep function, jitter source and attempt budget are
    injected so retry behaviour is testable without a network or a real wait.
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
        jitter: Callable[[], float] = random.random,
        operation: str = EMBED_INGEST,
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
        self._jitter = jitter
        self._operation = operation

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def space(self) -> EmbeddingSpace:
        """The vector space this client's vectors belong to (RAG-06)."""
        return EmbeddingSpace(
            provider=OPENAI_PROVIDER,
            model=self._model,
            dimensions=self._dimensions,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts, returning one vector per input in order.

        Order is part of the contract: the caller pairs the results back with
        the chunks that produced them, and a reordered response would attach
        each chunk's meaning to its neighbour.
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = await self.embed_batch(texts[start : start + MAX_BATCH])
            vectors.extend(batch.vectors)
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        """Embed a single text, for the query side of retrieval."""
        batch = await self.embed_batch([text])
        return batch.vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> EmbeddingBatch:
        """One provider request for at most `MAX_BATCH` texts."""
        if not texts:
            return EmbeddingBatch(vectors=[], input_tokens=None, characters=0)
        if len(texts) > MAX_BATCH:
            raise ValidationError(f"At most {MAX_BATCH} texts may be embedded in one request.")
        if any(not text.strip() for text in texts):
            raise ValidationError("An embedding input cannot be empty.")

        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            # Requested explicitly so the provider cannot return a width the
            # column will not accept. text-embedding-3-* support truncation.
            "dimensions": self._dimensions,
            "encoding_format": "float",
        }
        limit = len(texts) * self._dimensions * RESPONSE_BYTES_PER_VALUE + RESPONSE_OVERHEAD_BYTES
        call = ProviderCall(provider=Provider.OPENAI, operation=self._operation)
        body = await self._post(payload, call=call, limit=limit)
        try:
            vectors = self._vectors(body, expected=len(texts))
        except EmbeddingError:
            await call.attempt(CallOutcome.FAILURE)
            await call.record(CallOutcome.FAILURE)
            logger.warning(
                "openai.embeddings_invalid",
                extra={"inputs": len(texts), "operation": self._operation},
            )
            raise
        await call.attempt(CallOutcome.SUCCESS)
        await call.record(CallOutcome.SUCCESS)
        return EmbeddingBatch(
            vectors=vectors,
            input_tokens=_input_tokens(body),
            characters=sum(len(text) for text in texts),
        )

    async def _post(
        self,
        payload: dict[str, Any],
        *,
        call: ProviderCall,
        limit: int,
    ) -> dict[str, Any]:
        """Send, retrying what may pass. Returns a decoded 2xx body.

        The success attempt is counted by the caller, after the vectors have
        been validated: a 200 carrying a NaN is a failed attempt, not a
        successful one followed by a problem.
        """
        url = self._base_url + EMBEDDINGS_PATH
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
                        limit=limit if status < CLIENT_ERROR_FLOOR else MAX_ERROR_BODY_BYTES,
                        refuse=status < CLIENT_ERROR_FLOOR,
                    )
            except httpx.TransportError as error:
                await call.attempt(CallOutcome.UNAVAILABLE)
                if attempt >= self._max_attempts:
                    logger.warning(
                        "openai.embeddings_unreachable",
                        extra={"attempts": attempt, "operation": self._operation},
                    )
                    await call.record(CallOutcome.UNAVAILABLE)
                    raise EmbeddingError(EmbeddingFailure.PROVIDER_UNREACHABLE) from error
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            except _ResponseTooLargeError:
                await call.attempt(CallOutcome.FAILURE)
                logger.warning(
                    "openai.embeddings_response_too_large",
                    extra={"attempts": attempt, "limit_bytes": limit},
                )
                await call.record(CallOutcome.FAILURE)
                raise EmbeddingError(EmbeddingFailure.RESPONSE_TOO_LARGE) from None

            if status < CLIENT_ERROR_FLOOR:
                try:
                    return _decode(content)
                except EmbeddingError:
                    await call.attempt(CallOutcome.FAILURE)
                    await call.record(CallOutcome.FAILURE)
                    raise

            await call.attempt(_attempt_outcome(status))
            retryable = status == TOO_MANY_REQUESTS or status >= SERVER_ERROR_FLOOR
            if not retryable:
                self._log_failure(status, content, attempts=attempt)
                await call.record(CallOutcome.FAILURE)
                raise EmbeddingError(
                    _CLIENT_FAILURES.get(status, EmbeddingFailure.PROVIDER_INVALID_REQUEST)
                )

            delay = self._delay(attempt, hint)
            if attempt >= self._max_attempts or delay is None:
                self._log_failure(status, content, attempts=attempt, retry_after=hint)
                if status == TOO_MANY_REQUESTS:
                    await call.record(CallOutcome.RATE_LIMITED)
                    raise EmbeddingRateLimitedError()
                await call.record(CallOutcome.UNAVAILABLE)
                raise EmbeddingError(EmbeddingFailure.PROVIDER_UNAVAILABLE)
            await self._sleep(delay)
            attempt += 1

    def _delay(self, attempt: int, hint: float | None) -> float | None:
        """How long to wait before the next attempt, or None to stop retrying.

        A provider hint within the cap is honoured as given; one beyond it ends
        the retries, as in the Responses client.
        """
        if hint is not None:
            return hint if hint <= MAX_RETRY_AFTER_SECONDS else None
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        """Linear backoff with equal jitter, so workers that failed together part."""
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
        customer's own documents - and the message for a bad key quotes the key.
        """
        error: dict[str, Any] = {}
        try:
            body = json.loads(content) if content else {}
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        logger.warning(
            "openai.embeddings_failed",
            extra={
                "status": status,
                "attempts": attempts,
                "operation": self._operation,
                "provider_code": _closed(error.get("code")),
                "provider_type": _closed(error.get("type")),
                "retry_after_seconds": retry_after,
            },
        )

    def _vectors(self, body: dict[str, Any], *, expected: int) -> list[list[float]]:
        """Pull the vectors out, sorted by the index the provider assigned.

        Sorted rather than trusted in array order: the API documents an `index`
        on each item precisely because the order is not guaranteed, and getting
        this wrong would silently mislabel every chunk. Every index must be
        present exactly once.
        """
        data = body.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)

        ordered: dict[int, list[float]] = {}
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
            index = item.get("index")
            key = index if type(index) is int else position
            if key in ordered or not 0 <= key < expected:
                raise EmbeddingError(EmbeddingFailure.INVALID_EMBEDDING)
            ordered[key] = validate_vector(item.get("embedding"), dimensions=self._dimensions)
        return [ordered[key] for key in range(expected)]


def _decode(content: bytes) -> dict[str, Any]:
    try:
        body = json.loads(content)
    except ValueError as error:
        raise EmbeddingError(EmbeddingFailure.UNREADABLE_RESPONSE) from error
    if not isinstance(body, dict):
        raise EmbeddingError(EmbeddingFailure.UNREADABLE_RESPONSE)
    return body


def _input_tokens(body: dict[str, Any]) -> int | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens = usage.get("prompt_tokens")
    if type(tokens) is not int or not 0 <= tokens <= MAX_REPORTED_INPUT_TOKENS:
        return None
    return tokens


def _closed(value: object) -> str | None:
    """A provider code only if it looks like a code: short, and no spaces."""
    if isinstance(value, str) and len(value) <= 64 and " " not in value:
        return value
    return None


__all__ = [
    "EMBED_INGEST",
    "EMBED_QUERY",
    "MAX_BATCH",
    "PERMANENT_FAILURES",
    "EmbeddingBatch",
    "EmbeddingError",
    "EmbeddingFailure",
    "EmbeddingRateLimitedError",
    "EmbeddingsClient",
    "build_http_client",
    "embedding_failure",
    "validate_vector",
]
