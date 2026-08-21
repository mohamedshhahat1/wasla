"""Outbound WhatsApp Cloud API client.

Retry policy, and why it is narrow: the send endpoint takes no idempotency key,
so a retry can duplicate a customer-visible message. Only failures that
definitely did not send are retried.

| Failure | Retried | Reason |
| --- | --- | --- |
| 429 | yes | Rejected outright; nothing was sent |
| connection error | yes | No connection, so no request arrived |
| 5xx | no | May have been accepted; a duplicate reply is worse |
| read timeout | no | Same: the request may have landed |
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

GRAPH_BASE_URL: Final = "https://graph.facebook.com"
MESSAGING_PRODUCT: Final = "whatsapp"
REQUEST_TIMEOUT_SECONDS: Final = 10.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = 0.5
TOO_MANY_REQUESTS: Final = 429
SERVER_ERROR_FLOOR: Final = 500

MediaKind = Literal["image", "document", "audio", "video"]


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Meta's acknowledgement of one accepted message."""

    message_id: str
    recipient: str
    raw: dict[str, Any]


def build_http_client() -> httpx.AsyncClient:
    """An HTTP client with a bounded timeout.

    A client without a timeout will eventually hang a worker on a provider
    stall, so the timeout is set here rather than left to callers.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS))


class WhatsAppClient:
    """Sends messages through the WhatsApp Cloud API.

    The HTTP client, sleep function and attempt budget are injected so the retry
    behaviour can be tested without a network or a real wait.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        access_token: str,
        api_version: str = "v21.0",
        max_attempts: int = MAX_ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not access_token:
            raise ValidationError("A WhatsApp access token is required to send messages.")
        self._http = http
        self._access_token = access_token
        self._api_version = api_version
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    async def send_text(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        preview_url: bool = False,
    ) -> SentMessage:
        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "text", "text": {"body": body, "preview_url": preview_url}},
        )

    async def send_media(
        self,
        *,
        phone_number_id: str,
        to: str,
        kind: MediaKind,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> SentMessage:
        """Send media by hosted link or by uploaded media id, never both."""
        if bool(link) == bool(media_id):
            raise ValidationError("Provide exactly one of a media link or a media id.")

        media: dict[str, Any] = {"link": link} if link else {"id": media_id}
        if caption and kind != "audio":
            # Meta rejects captions on audio; sending one fails the whole message.
            media["caption"] = caption
        if filename and kind == "document":
            media["filename"] = filename

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": kind, kind: media},
        )

    async def send_location(
        self,
        *,
        phone_number_id: str,
        to: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
    ) -> SentMessage:
        location: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
        if name:
            location["name"] = name
        if address:
            location["address"] = address

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "location", "location": location},
        )

    async def send_buttons(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
    ) -> SentMessage:
        """Reply buttons, as `(id, title)` pairs. Meta allows at most three."""
        if not buttons or len(buttons) > 3:
            raise ValidationError("Provide between one and three reply buttons.")

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": button_id, "title": title}}
                            for button_id, title in buttons
                        ]
                    },
                },
            },
        )

    async def send_list(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict[str, Any]],
    ) -> SentMessage:
        if not sections:
            raise ValidationError("A list message needs at least one section.")

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {"button": button_text, "sections": sections},
                },
            },
        )

    async def send_template(
        self,
        *,
        phone_number_id: str,
        to: str,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SentMessage:
        """Templates are the only way to open a conversation outside the 24-hour window."""
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components

        return await self._send(
            phone_number_id=phone_number_id,
            to=to,
            content={"type": "template", "template": template},
        )

    async def mark_read(self, *, phone_number_id: str, message_id: str) -> None:
        """Show the customer a read receipt."""
        await self._post(
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": MESSAGING_PRODUCT,
                "status": "read",
                "message_id": message_id,
            },
        )

    async def _send(
        self,
        *,
        phone_number_id: str,
        to: str,
        content: dict[str, Any],
    ) -> SentMessage:
        payload = {
            "messaging_product": MESSAGING_PRODUCT,
            "recipient_type": "individual",
            "to": to,
            **content,
        }
        response = await self._post(phone_number_id=phone_number_id, payload=payload)
        return SentMessage(
            message_id=self._message_id(response),
            recipient=self._recipient(response) or to,
            raw=response,
        )

    async def _post(self, *, phone_number_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{GRAPH_BASE_URL}/{self._api_version}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        attempt = 1
        while True:
            try:
                response = await self._http.post(url, json=payload, headers=headers)
            except httpx.ConnectError as error:
                # Nothing reached Meta, so a retry cannot duplicate anything.
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.send_unreachable", extra={"attempts": attempt})
                    raise ExternalServiceError("WhatsApp could not be reached.") from error
                await self._backoff(attempt)
                attempt += 1
                continue
            except httpx.TimeoutException as error:
                # The request may have landed. Retrying risks a second message.
                logger.warning("whatsapp.send_timed_out", extra={"attempts": attempt})
                raise ExternalServiceError("WhatsApp did not respond in time.") from error

            if response.status_code == TOO_MANY_REQUESTS:
                if attempt >= self._max_attempts:
                    logger.warning("whatsapp.send_rate_limited", extra={"attempts": attempt})
                    raise RateLimitedError("WhatsApp is rate limiting this account.")
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code >= SERVER_ERROR_FLOOR:
                # Deliberately not retried: the message may have been accepted.
                self._log_failure(response)
                raise ExternalServiceError("WhatsApp rejected the message.")

            if response.status_code >= httpx.codes.BAD_REQUEST:
                self._log_failure(response)
                raise ExternalServiceError("WhatsApp rejected the message.")

            return self._decode(response)

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self._backoff_seconds * attempt)

    def _log_failure(self, response: httpx.Response) -> None:
        """Log Meta's own error code, but never hand its text to the caller.

        Provider error text can echo fragments of the request, and this client
        holds a live platform credential.
        """
        error = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]

        logger.warning(
            "whatsapp.send_failed",
            extra={
                "status": response.status_code,
                "meta_code": error.get("code"),
                "meta_type": error.get("type"),
                "meta_subcode": error.get("error_subcode"),
            },
        )

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            raise ExternalServiceError("WhatsApp returned an unreadable response.") from error
        if not isinstance(body, dict):
            raise ExternalServiceError("WhatsApp returned an unexpected response.")
        return body

    def _message_id(self, body: dict[str, Any]) -> str:
        messages = body.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = messages[0].get("id")
            if isinstance(message_id, str) and message_id:
                return message_id
        # Accepted but unidentifiable is not usable: statuses arrive keyed on id.
        raise ExternalServiceError("WhatsApp accepted the message without an identifier.")

    def _recipient(self, body: dict[str, Any]) -> str | None:
        contacts = body.get("contacts")
        if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
            recipient = contacts[0].get("wa_id")
            if isinstance(recipient, str):
                return recipient
        return None
