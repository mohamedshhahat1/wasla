"""Resend, spoken to directly over HTTPS.

One JSON POST. There is deliberately no SDK dependency: the API surface this
project needs is a single endpoint, and a package that saves twenty lines is
not worth a supply-chain entry the scanner has to watch and the lockfile has
to pin (ADR-017's reasoning, applied in the other direction).

The API key appears in exactly one place - the Authorization header of the
request being made - and never in an error, a log line, or a repr. Errors
coming back are translated into the internal vocabulary before they leave
this module, with the body truncated hard: provider error strings have a
habit of quoting the request back, and this request is credentialed.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.core.net import UnsafeUrlError, build_guarded_client
from app.integrations.email.base import EmailMessage, EmailSendResult, EmailSendState

logger = get_logger(__name__)

RESEND_ENDPOINT: Final = "https://api.resend.com/emails"
DEFAULT_TIMEOUT_SECONDS: Final = 15.0
# How much of a provider error body may survive into a result. Enough to act
# on, too little to exfiltrate through.
MAX_ERROR_LENGTH: Final = 300


def _classify(status_code: int) -> EmailSendState:
    """Whether a refusal is worth retrying.

    429 and the 5xx family are the provider's problem or the provider's
    moment, and the message may well go through later. Everything else in the
    4xx family is this request being wrong - an invalid recipient, a rejected
    sender domain - and asking again cannot fix a request.
    """
    if status_code == 429 or status_code >= 500:
        return EmailSendState.TRANSIENT_FAILURE
    return EmailSendState.PERMANENT_FAILURE


class ResendEmailProvider:
    """Sends through the Resend REST API."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds
        # Injectable so the suite can drive every failure class without a
        # network, the same way the WhatsApp client takes one.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        """The HTTP client every Resend call uses.

        `build_guarded_client`, the same constructor OpenAI, WhatsApp, Google
        and Paymob use, so the answer to "which outbound clients are guarded?"
        is "all of them" rather than a list that goes stale (ADR-067). This
        integration was the stale entry: it passed `transport=self._transport`,
        which is `None` in production, so every real send went out on httpx's
        default transport with no address check at all.

        The URL is a constant, so what the guard closes is narrow - a hijacked
        or poisoned resolver for `api.resend.com` - and the request it protects
        carries `RESEND_API_KEY` in an Authorization header. Narrow is not
        absent, and the point of the rule is that the question has one answer.

        Redirects stay off, which is the httpx default and what
        `GuardedTransport` assumes: it judges one hop, so a client following
        the next one would follow it unjudged.

        A `transport` is supplied only by tests, which need a mock socket
        rather than a real one. Production never passes one, so the guarded
        path is the only path a deployment can take.
        """
        timeout = httpx.Timeout(self._timeout)
        if self._transport is not None:
            return httpx.AsyncClient(timeout=timeout, transport=self._transport)
        return build_guarded_client(timeout=timeout)

    @property
    def name(self) -> str:
        return "resend"

    async def send(
        self,
        message: EmailMessage,
        *,
        idempotency_key: str | None = None,
    ) -> EmailSendResult:
        """One attempt at delivery, classified.

        The idempotency key is forwarded because Resend honours one, but the
        outbox does not rely on it: provider-side idempotency is a courtesy
        window, and the durable guarantee is the database key (ADR-042).
        """
        payload: dict[str, Any] = {
            "from": message.sender,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.text,
        }
        if message.html is not None:
            payload["html"] = message.html
        if message.reply_to is not None:
            payload["reply_to"] = message.reply_to

        headers = {"Authorization": f"Bearer {self._api_key}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        try:
            async with self._client() as client:
                response = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
        except httpx.TimeoutException:
            return EmailSendResult(
                state=EmailSendState.TRANSIENT_FAILURE,
                provider=self.name,
                error_code="timeout",
                error_message="the provider did not answer in time",
            )
        except UnsafeUrlError:
            # The guard refused the destination: `api.resend.com` resolved to
            # something that is not publicly routable. Translated here and
            # **not** allowed to escape, for the reason `PaymobProvider._post`
            # gives - `UnsafeUrlError` is deliberately not a `WaslaError`, so
            # uncaught it becomes a 500 from whatever route happened to trigger
            # a send, which is the wrong report for "this deployment cannot
            # safely reach its email provider".
            #
            # Transient, because the outbox's alternative is marking a message
            # permanently undeliverable over what is usually a resolver having
            # a bad minute. Which address was refused stays out of the result,
            # following the argument `app/core/net.py` makes for its own log.
            logger.warning(
                "email.destination_refused",
                extra={"event": "email.destination_refused", "provider": self.name},
            )
            return EmailSendResult(
                state=EmailSendState.TRANSIENT_FAILURE,
                provider=self.name,
                error_code="transport_error",
                error_message="the provider host did not resolve to a public address",
            )
        except httpx.HTTPError as error:
            # Transport-level: DNS, refused connection, TLS. The provider was
            # never reached, so the message is retryable by definition. The
            # exception type is kept and its text is not: httpx errors quote
            # the request they were carrying.
            return EmailSendResult(
                state=EmailSendState.TRANSIENT_FAILURE,
                provider=self.name,
                error_code="transport_error",
                error_message=type(error).__name__,
            )

        if response.is_success:
            provider_message_id = self._message_id(response)
            return EmailSendResult(
                state=EmailSendState.SENT,
                provider=self.name,
                provider_message_id=provider_message_id,
            )

        state = _classify(response.status_code)
        error_code, error_message = self._error_details(response)
        logger.warning(
            "email.provider_refused",
            extra={
                "event": "email.provider_refused",
                "provider": self.name,
                "status_code": response.status_code,
                "error_code": error_code,
                "retryable": state is EmailSendState.TRANSIENT_FAILURE,
            },
        )
        return EmailSendResult(
            state=state,
            provider=self.name,
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _message_id(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        identifier = body.get("id") if isinstance(body, dict) else None
        return str(identifier) if identifier else None

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str, str | None]:
        """A bounded description of a refusal, safe to store and to log."""
        error_code = f"http_{response.status_code}"
        message: str | None = None
        try:
            body = response.json()
        except ValueError:
            return error_code, None
        if isinstance(body, dict):
            name = body.get("name")
            if isinstance(name, str) and name:
                error_code = name[:100]
            detail = body.get("message")
            if isinstance(detail, str) and detail:
                message = detail[:MAX_ERROR_LENGTH]
        return error_code, message
