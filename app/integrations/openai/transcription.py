"""Speech to text.

A separate client rather than a method on `ResponsesClient`, because it is a
genuinely different endpoint: multipart rather than JSON, a file rather than a
conversation, and no tools, instructions or history. Sharing a class would mean
one object with two unrelated request shapes.

The retry policy is the Responses client's, and for the same reason: a repeated
transcription costs tokens but reaches no customer, so an ambiguous failure is a
money question rather than a correctness one. Everything transient is retried.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
TRANSCRIPTION_PATH: Final = "/audio/transcriptions"
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 1.0
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500
CLIENT_ERROR_FLOOR: Final = 400

# The provider requires a filename to infer the container format, and the bytes
# arriving from WhatsApp have no name of their own that is safe to reuse. The
# extension is chosen from the mime type rather than from anything a customer
# sent.
AUDIO_EXTENSIONS: Final[dict[str, str]] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/amr": "amr",
}


@dataclass(frozen=True, slots=True)
class Transcript:
    """What was said, and in what language if the provider could tell."""

    text: str
    language: str | None = None


class TranscriptionClient:
    """Turns recorded audio into text.

    The HTTP client, sleep function and attempt budget are injected so retry
    behaviour is testable without a network or a real wait, as everywhere else
    in this package.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        model: str,
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
        self._base_url = base_url.rstrip("/")
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    async def transcribe(
        self,
        *,
        content: bytes,
        mime_type: str | None = None,
        language: str | None = None,
    ) -> Transcript:
        """Transcribe one recording.

        `language` is a hint, not a constraint. It is left unset by default:
        this product's customers switch between Arabic and English inside a
        single sentence, and forcing one of them makes the other come back as
        nonsense rather than as a translation.
        """
        if not content:
            raise ValidationError("There is no audio to transcribe.")

        extension = AUDIO_EXTENSIONS.get((mime_type or "").lower(), "ogg")
        files = {"file": (f"audio.{extension}", content, mime_type or "audio/ogg")}
        data: dict[str, Any] = {"model": self._model, "response_format": "json"}
        if language:
            data["language"] = language

        body = await self._post(files=files, data=data)
        text = body.get("text")
        if not isinstance(text, str):
            raise ExternalServiceError("The transcription service returned no text.")

        spoken = body.get("language")
        return Transcript(
            text=text.strip(),
            language=spoken if isinstance(spoken, str) and spoken else None,
        )

    async def _post(self, *, files: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{TRANSCRIPTION_PATH}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        attempt = 1
        while True:
            try:
                response = await self._http.post(url, data=data, files=files, headers=headers)
            except httpx.HTTPError as error:
                if attempt >= self._max_attempts:
                    logger.warning("transcription.unreachable", extra={"attempts": attempt})
                    message = "The transcription service is unavailable."
                    raise ExternalServiceError(message) from error
                await self._backoff(attempt)
                attempt += 1
                continue

            retryable = (
                response.status_code == TOO_MANY_REQUESTS
                or response.status_code >= SERVER_ERROR_FLOOR
            )
            if retryable and attempt < self._max_attempts:
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code == TOO_MANY_REQUESTS:
                raise RateLimitedError("The transcription service is rate limiting this key.")
            if response.status_code >= CLIENT_ERROR_FLOOR:
                # The provider's error text can echo the request. Only the
                # status is logged, and nothing of it reaches the caller.
                logger.warning(
                    "transcription.rejected",
                    extra={"status": response.status_code},
                )
                raise ExternalServiceError("The transcription service rejected this recording.")

            return self._decode(response)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_seconds * attempt)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            message = "The transcription service returned an unreadable body."
            raise ExternalServiceError(message) from error
        if not isinstance(body, dict):
            raise ExternalServiceError("The transcription service returned an unexpected body.")
        return body
