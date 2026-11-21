"""The Graph calls of WhatsApp Embedded Signup - and the only place their URLs are built.

Five calls, each the step Meta's onboarding pages give a Tech Provider's server
(read 2026-11-21; ADR-204):

- **exchange** - `GET /oauth/access_token` with the app's id and secret and the
  code the popup returned, for the customer's business token (S4, EXT-ES-10).
  The code lives five minutes (S3, EXT-ES-09) and is single-use, so the
  exchange is never retried.
- **subscribe / unsubscribe** - `POST` / `DELETE /{messaging_account_id}/
  subscribed_apps` with the business token (S4, S10, EXT-ES-13). Without the
  subscription no webhook ever arrives for the customer's number.
- **register** - `POST /{phone_number_id}/register` with a six-digit PIN (S4,
  EXT-ES-14), for a number that is NOT on the WhatsApp Business app (S5:
  "skip the phone number registration step, as the number is already
  registered", EXT-ES-15).
- **coexistence** - `GET /{phone_number_id}?fields=is_on_biz_app,platform_type`
  (S5, EXT-ES-16). Meta's answer, not the browser's, decides the mode.
- **sync** - `POST /{phone_number_id}/smb_app_data` with `sync_type`
  `smb_app_state_sync` or `history` (S5, EXT-ES-24), each once, within 24 hours.

Every call goes through the guarded client, on `META_ONBOARDING_API_VERSION`,
and nothing here logs a URL, a token, a code, a secret or a PIN: Meta's error
text quotes requests back, so only its numeric code is kept (as in
`ownership.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

import httpx

from app.core.exceptions import ExternalServiceError, WaslaError
from app.core.logging import get_logger
from app.core.net import build_guarded_client

logger = get_logger(__name__)

GRAPH_BASE_URL: Final = "https://graph.facebook.com"
# A person is waiting on every call but the syncs, as on the ownership read.
ONBOARDING_TIMEOUT_SECONDS: Final = 10.0
PLATFORM_CLOUD_API: Final = "CLOUD_API"
SYNC_CONTACTS: Final = "smb_app_state_sync"
SYNC_HISTORY: Final = "history"
SYNC_TYPES: Final = frozenset({SYNC_CONTACTS, SYNC_HISTORY})
# The words Meta uses for a code past its five minutes. The exchange's error
# shape is not documented (EXT-ES-09b): an OAuthException whose message says
# the code expired is read as expired, every other one as invalid.
_EXPIRED_WORDS: Final = ("expired", "has expired")


class EmbeddedSignupCodeInvalidError(WaslaError):
    """Meta refused the code: not one it issued to this app, or already used."""

    status_code = 422
    error_code = "embedded_signup_code_invalid"
    message = "Meta did not accept the sign-up code. Start the connection again."


class EmbeddedSignupCodeExpiredError(WaslaError):
    """The code was older than Meta's five minutes (EXT-ES-09)."""

    status_code = 422
    error_code = "embedded_signup_code_expired"
    message = "The sign-up code from Meta expired. Start the connection again."


class OnboardingStepError(Exception):
    """A subscribe, register, read or sync call Meta did not accept.

    Not a `WaslaError`: these happen after the connection exists, and a caller
    records them as the connection's health rather than answering a request
    with them. `meta_code` is Meta's numeric code, never its text.
    """

    def __init__(self, step: str, *, status: int | None, meta_code: int | None) -> None:
        super().__init__(f"{step} failed")
        self.step = step
        self.status = status
        self.meta_code = meta_code

    @property
    def is_auth(self) -> bool:
        """Meta's code 190 is an invalid or expired token; 401 the same at HTTP."""
        return self.meta_code == 190 or self.status == httpx.codes.UNAUTHORIZED


class OnboardingStep(StrEnum):
    EXCHANGE = "exchange"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    REGISTER = "register"
    COEXISTENCE = "coexistence"
    SYNC = "sync"


@dataclass(frozen=True, slots=True)
class ExchangedToken:
    """The customer's business token. Never logged, returned or stored unsealed."""

    access_token: str
    #: Only when Meta states one (EXT-ES-12: not documented, so usually None).
    expires_in: int | None

    def __repr__(self) -> str:  # pragma: no cover - keeps the token out of any log
        return "ExchangedToken(<redacted>)"


@dataclass(frozen=True, slots=True)
class NumberPlatform:
    """Meta's answer about which WhatsApp the number is on (EXT-ES-16)."""

    is_on_biz_app: bool
    platform_type: str | None

    @property
    def coexistence(self) -> bool:
        """Both apps on one number: on the Business app and served by the Cloud API (CX-02)."""
        return self.is_on_biz_app and self.platform_type == PLATFORM_CLOUD_API


class OnboardingClient(Protocol):
    """What the onboarding service asks of Meta. A protocol so tests answer without a network."""

    async def exchange_code(self, code: str) -> ExchangedToken: ...

    async def subscribe(self, *, token: str, messaging_account_id: str) -> None: ...

    async def unsubscribe(self, *, token: str, messaging_account_id: str) -> None: ...

    async def register(self, *, token: str, phone_number_id: str, pin: str) -> None: ...

    async def read_platform(self, *, token: str, phone_number_id: str) -> NumberPlatform: ...

    async def request_sync(self, *, token: str, phone_number_id: str, sync_type: str) -> str: ...


class MetaOnboardingClient:
    """The five calls over Meta's Graph API. `http` is injected in tests."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_version: str,
        http: httpx.AsyncClient | None = None,
        base_url: str = GRAPH_BASE_URL,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_version = api_version
        self._http = http
        self._base_url = base_url.rstrip("/")

    def _url(self, *parts: str) -> str:
        return "/".join((self._base_url, self._api_version, *parts))

    async def exchange_code(self, code: str) -> ExchangedToken:
        """The code for the business token, server to server, once (EX-03, EX-04).

        The secret travels in the query only for this call, as S4 shows it; the
        URL is never logged. Not retried: a code is single-use.
        """
        cleaned = code.strip()
        if not cleaned:
            raise EmbeddedSignupCodeInvalidError()
        response = await self._send(
            OnboardingStep.EXCHANGE,
            "GET",
            self._url("oauth", "access_token"),
            params={
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "code": cleaned,
            },
        )
        if response.status_code != httpx.codes.OK:
            error = _meta_error(response)
            self._log_refusal(OnboardingStep.EXCHANGE, response.status_code, error)
            if error.get("type") == "OAuthException" or response.status_code in (400, 401):
                message = str(error.get("message") or "").lower()
                if any(word in message for word in _EXPIRED_WORDS):
                    raise EmbeddedSignupCodeExpiredError()
                raise EmbeddedSignupCodeInvalidError()
            raise ExternalServiceError("Meta could not be reached to finish the connection.")
        body = _json(response)
        token = body.get("access_token") if body is not None else None
        if not isinstance(token, str) or not token.strip():
            self._log_refusal(OnboardingStep.EXCHANGE, response.status_code, {})
            raise ExternalServiceError("Meta's answer to the sign-up code could not be read.")
        expires = body.get("expires_in") if body is not None else None
        return ExchangedToken(
            access_token=token.strip(),
            expires_in=(
                expires if isinstance(expires, int) and not isinstance(expires, bool) else None
            ),
        )

    async def subscribe(self, *, token: str, messaging_account_id: str) -> None:
        """Subscribe this app to the customer's webhooks (SB-01, EXT-ES-13)."""
        await self._expect_success(
            OnboardingStep.SUBSCRIBE,
            "POST",
            self._url(messaging_account_id, "subscribed_apps"),
            token=token,
        )

    async def unsubscribe(self, *, token: str, messaging_account_id: str) -> None:
        """Stop the customer's webhooks to this app, at release (RL-02)."""
        await self._expect_success(
            OnboardingStep.UNSUBSCRIBE,
            "DELETE",
            self._url(messaging_account_id, "subscribed_apps"),
            token=token,
        )

    async def register(self, *, token: str, phone_number_id: str, pin: str) -> None:
        """Register a new Cloud API number with its two-step PIN (SB-09, EXT-ES-14)."""
        await self._expect_success(
            OnboardingStep.REGISTER,
            "POST",
            self._url(phone_number_id, "register"),
            token=token,
            body={"messaging_product": "whatsapp", "pin": pin},
        )

    async def read_platform(self, *, token: str, phone_number_id: str) -> NumberPlatform:
        """Whether the number is also on the WhatsApp Business app (CX-01, EXT-ES-16)."""
        response = await self._send(
            OnboardingStep.COEXISTENCE,
            "GET",
            self._url(phone_number_id),
            token=token,
            params={"fields": "is_on_biz_app,platform_type"},
        )
        body = self._require_ok(OnboardingStep.COEXISTENCE, response)
        on_app = body.get("is_on_biz_app")
        platform = body.get("platform_type")
        if not isinstance(on_app, bool):
            raise OnboardingStepError(
                OnboardingStep.COEXISTENCE, status=response.status_code, meta_code=None
            )
        return NumberPlatform(
            is_on_biz_app=on_app,
            platform_type=platform if isinstance(platform, str) else None,
        )

    async def request_sync(self, *, token: str, phone_number_id: str, sync_type: str) -> str:
        """Ask Meta for the one-time contacts or history sync; Meta's request id (SY-01)."""
        if sync_type not in SYNC_TYPES:
            raise ValueError(f"unknown sync type {sync_type!r}")
        response = await self._send(
            OnboardingStep.SYNC,
            "POST",
            self._url(phone_number_id, "smb_app_data"),
            token=token,
            body={"messaging_product": "whatsapp", "sync_type": sync_type},
        )
        body = self._require_ok(OnboardingStep.SYNC, response)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise OnboardingStepError(
                OnboardingStep.SYNC, status=response.status_code, meta_code=None
            )
        return request_id[:255]

    # ---------------------------------------------------------------- plumbing

    async def _expect_success(
        self,
        step: OnboardingStep,
        method: str,
        url: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
    ) -> None:
        response = await self._send(step, method, url, token=token, body=body)
        answered = self._require_ok(step, response)
        if answered.get("success") is not True:
            self._log_refusal(step, response.status_code, {})
            raise OnboardingStepError(step, status=response.status_code, meta_code=None)

    def _require_ok(self, step: OnboardingStep, response: httpx.Response) -> dict[str, Any]:
        if response.status_code != httpx.codes.OK:
            error = _meta_error(response)
            self._log_refusal(step, response.status_code, error)
            code = error.get("code")
            raise OnboardingStepError(
                step,
                status=response.status_code,
                meta_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
            )
        body = _json(response)
        if body is None:
            self._log_refusal(step, response.status_code, {})
            raise OnboardingStepError(step, status=response.status_code, meta_code=None)
        return body

    async def _send(
        self,
        step: OnboardingStep,
        method: str,
        url: str,
        *,
        token: str | None = None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            if self._http is not None:
                return await self._request(self._http, method, url, headers, params, body)
            async with build_guarded_client(
                timeout=httpx.Timeout(ONBOARDING_TIMEOUT_SECONDS)
            ) as http:
                return await self._request(http, method, url, headers, params, body)
        except httpx.HTTPError as error:
            logger.warning(
                "whatsapp.onboarding_unreachable",
                extra={
                    "event": "whatsapp.onboarding_unreachable",
                    "step": step.value,
                    "reason": type(error).__name__,
                },
            )
            if step is OnboardingStep.EXCHANGE:
                raise ExternalServiceError(
                    "Meta could not be reached to finish the connection."
                ) from error
            raise OnboardingStepError(step, status=None, meta_code=None) from error

    @staticmethod
    async def _request(
        http: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        body: dict[str, Any] | None,
    ) -> httpx.Response:
        return await http.request(
            method,
            url,
            params=params,
            json=body,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(ONBOARDING_TIMEOUT_SECONDS),
        )

    @staticmethod
    def _log_refusal(step: OnboardingStep, status: int, error: dict[str, Any]) -> None:
        """Meta's numeric code and subcode, never its text (which quotes the request)."""
        logger.warning(
            "whatsapp.onboarding_refused",
            extra={
                "event": "whatsapp.onboarding_refused",
                "step": step.value,
                "status": status,
                "meta_code": error.get("code"),
                "meta_subcode": error.get("error_subcode"),
            },
        )


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _meta_error(response: httpx.Response) -> dict[str, Any]:
    body = _json(response)
    error = body.get("error") if body is not None else None
    return error if isinstance(error, dict) else {}


__all__ = [
    "PLATFORM_CLOUD_API",
    "SYNC_CONTACTS",
    "SYNC_HISTORY",
    "EmbeddedSignupCodeExpiredError",
    "EmbeddedSignupCodeInvalidError",
    "ExchangedToken",
    "MetaOnboardingClient",
    "NumberPlatform",
    "OnboardingClient",
    "OnboardingStep",
    "OnboardingStepError",
]
