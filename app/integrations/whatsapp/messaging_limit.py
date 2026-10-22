"""Reading a number's messaging limit from Meta (LIM-01, ADR-146).

`GET /{phone-number-id}?fields=whatsapp_business_manager_messaging_limit`
answers the owning business portfolio's limit - `{"whatsapp_business_manager_messaging_limit":
"TIER_250", "id": "..."}` ("Messaging limits", developers.facebook.com, read
2026-10-16). The field exists on Graph API **v24.0 and newer** (changelog
2025-10-08); `messaging_limit_tier` is deprecated.

**This one call carries its own version.** Wasla's configured Graph version
(`META_API_VERSION`) is older, and raising it changes every request Wasla makes
- a decision of its own, not this stage's. The read is a single GET of a single
field, so it names the version Meta documents for that field
(`META_MESSAGING_LIMIT_API_VERSION`, default v24.0) and nothing else moves.

The credential is the number's own (ADR-034) - the same one sends use - and,
like the ownership read, a failure of any kind is one answer: the limit could
not be read. The caller keeps the value it already had (LIM-01).
"""

from __future__ import annotations

import json
from typing import Any, Final, Protocol

import httpx

from app.core.logging import get_logger
from app.core.net import build_guarded_client

logger = get_logger(__name__)

GRAPH_BASE_URL: Final = "https://graph.facebook.com"
#: The version Meta documents `whatsapp_business_manager_messaging_limit` on.
DEFAULT_LIMIT_API_VERSION: Final = "v24.0"
LIMIT_FIELD: Final = "whatsapp_business_manager_messaging_limit"
READ_TIMEOUT_SECONDS: Final = 8.0
#: Meta's tier strings are short; anything longer is not one of them.
MAX_VALUE_LENGTH: Final = 40


class MessagingLimitUnreadableError(Exception):
    """The limit could not be read. Nothing more specific is ever said."""


class MessagingLimitReader(Protocol):
    """The one question: what is this number's portfolio limit, as Meta words it."""

    async def read(self, *, access_token: str, phone_number_id: str) -> str:
        """Meta's value for the limit (`TIER_250`, ...); raises `MessagingLimitUnreadableError`."""
        ...  # pragma: no cover - protocol declaration


class MetaMessagingLimitReader:
    """Asks Meta, on the documented version, with the number's own credential."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_version: str = DEFAULT_LIMIT_API_VERSION,
        base_url: str = GRAPH_BASE_URL,
    ) -> None:
        self._http = http
        self._api_version = api_version
        self._base_url = base_url.rstrip("/")

    async def read(self, *, access_token: str, phone_number_id: str) -> str:
        token = access_token.strip()
        number = phone_number_id.strip()
        if not token or not number:
            raise MessagingLimitUnreadableError()
        if self._http is not None:
            return await self._read(self._http, token, number)
        async with build_guarded_client(timeout=httpx.Timeout(READ_TIMEOUT_SECONDS)) as http:
            return await self._read(http, token, number)

    async def _read(self, http: httpx.AsyncClient, token: str, number: str) -> str:
        try:
            response = await http.get(
                f"{self._base_url}/{self._api_version}/{number}",
                params={"fields": LIMIT_FIELD},
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                timeout=httpx.Timeout(READ_TIMEOUT_SECONDS),
            )
        except httpx.HTTPError as error:
            logger.warning(
                "whatsapp.messaging_limit_unreadable",
                extra={
                    "event": "whatsapp.messaging_limit_unreadable",
                    "reason": type(error).__name__,
                },
            )
            raise MessagingLimitUnreadableError() from error
        if response.status_code != httpx.codes.OK:
            logger.warning(
                "whatsapp.messaging_limit_unreadable",
                extra={
                    "event": "whatsapp.messaging_limit_unreadable",
                    "status": response.status_code,
                    "meta_code": _meta_code(response),
                },
            )
            raise MessagingLimitUnreadableError()
        try:
            body: Any = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise MessagingLimitUnreadableError() from error
        value = body.get(LIMIT_FIELD) if isinstance(body, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_VALUE_LENGTH:
            logger.warning(
                "whatsapp.messaging_limit_unreadable",
                extra={"event": "whatsapp.messaging_limit_unreadable", "reason": "no_value"},
            )
            raise MessagingLimitUnreadableError()
        return value.strip()


def _meta_code(response: httpx.Response) -> Any:
    """Meta's numeric error code, never its text (which can quote the request)."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code")
    return None


__all__ = [
    "DEFAULT_LIMIT_API_VERSION",
    "LIMIT_FIELD",
    "MessagingLimitReader",
    "MessagingLimitUnreadableError",
    "MetaMessagingLimitReader",
]
