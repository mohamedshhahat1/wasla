"""Request body and duration limits, enforced by the application itself.

Both of these are configured in nginx already (`client_max_body_size`,
`proxy_read_timeout`), and both are here anyway. The reason is simple: nginx is
one deployment topology, not a property of the software. Run the container
directly, put a different proxy in front, or reach it from inside the cluster,
and every one of those limits disappears — silently, and exactly in the
environment where nobody thinks to check.

**The body limit is enforced before the body is read.** A `Content-Length`
larger than the cap is refused without consuming the request, which is the whole
point: a limit applied after buffering has already spent the memory it exists to
protect. A request that lies about its length, or sends none at all, is counted
as it streams and cut off at the same cap.

**The timeout bounds the handler, not the client.** A slow customer on a bad
connection is not the problem; a handler that has been waiting on something for
two minutes is, and it is holding a database connection from a bounded pool
while it does.

**The cap depends on who is asking and for what (SEC-02).** A JSON body is
parsed in full before validation and before any route dependency - including
the rate limiter - runs, at roughly nine times its size in Python objects. One
32 MiB cap sized for attachments therefore let an anonymous client make the
process allocate ~270 MiB per request, already-rate-limited or not. So:

* a request that carries no verifiable access token gets the small
  `MAX_JSON_REQUEST_BYTES` cap (64 KiB) on every route - that is the whole of
  what an unauthenticated caller may make this process parse;
* a request that does gets `MAX_AUTHENTICATED_REQUEST_BYTES` (1 MiB), which
  holds every current JSON schema at its maximum even with every character
  escaped (`test_body_allowances.py` computes this from the live schema);
* exactly two routes take more, and only from a signed-in caller: the
  attachment upload (`MAX_REQUEST_BYTES`) and knowledge-document submission
  (`MAX_DOCUMENT_REQUEST_BYTES`). They are named in `UPLOAD_ALLOWANCES`, and a
  test asserts every entry still matches a real route;
* the webhooks keep their own 1 MiB cap, whoever is asking.

"Verifiable" means the signature, issuer, audience and expiry check out - the
same `decode_token` the routes use. It chooses a *capacity*, never grants
access: the route still authenticates the caller, re-reads the token version
and checks membership. A revoked-but-unexpired token therefore buys at most the
authenticated cap, which is the price of not touching the database here.

The WhatsApp webhook is exempt from the timeout for the same reason it is exempt
from rate limiting (ADR-032): a timed-out webhook is a non-2xx, Meta retries it,
and eventually the subscription is disabled. It keeps the body limit, because a
body cap protects memory rather than shedding load.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import AuthenticationError, error_payload
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger(__name__)

# Paths that must never be cut off mid-flight. Prefix-matched, so the
# verification handshake and the delivery endpoint are both covered.
TIMEOUT_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/api/v1/webhooks",)
# Same path, a different rule: exempt from the timeout, and held to a
# *tighter* body cap than everything else.
WEBHOOK_PREFIX: Final = "/api/v1/webhooks"

# The routes allowed a body larger than the authenticated default, and which
# cap each takes. Matched on method and the path *below* the API prefix. Kept
# short on purpose: every entry is an endpoint anybody holding a valid token can
# make the process buffer that much for.
UPLOAD: Final = "upload"
DOCUMENT: Final = "document"


@dataclass(frozen=True, slots=True)
class BodyAllowance:
    """One route permitted a larger body, and which cap applies to it."""

    method: str
    path: re.Pattern[str]
    cap: str


UPLOAD_ALLOWANCES: Final[tuple[BodyAllowance, ...]] = (
    # Multipart attachment upload; bounded again by `MEDIA_MAX_BYTES` inside.
    BodyAllowance("POST", re.compile(r"/conversations/[^/]+/messages/media"), UPLOAD),
    # A knowledge document as text or base64, up to 400 000 characters.
    BodyAllowance("POST", re.compile(r"/knowledge/bases/[^/]+/documents"), DOCUMENT),
)

_BEARER: Final = "bearer "


async def _refuse(
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
) -> None:
    """Answer with the project's error envelope, from outside the app.

    Middleware at this level runs before the exception handlers, so the envelope
    is built by hand rather than raised - a caller should not be able to tell
    which layer refused them.
    """
    import json

    body = json.dumps(error_payload(code, message)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Refuses a request body larger than the cap.

    Pure ASGI rather than `BaseHTTPMiddleware`, because the declared length has
    to be checked *before* anything reads the stream, and the streaming case has
    to intercept each chunk as it arrives. A middleware that awaits `request.body()`
    to measure it has already done the thing the limit exists to prevent.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        webhook_max_bytes: int | None = None,
        json_max_bytes: int | None = None,
        authenticated_max_bytes: int | None = None,
        document_max_bytes: int | None = None,
        api_prefix: str = "/api/v1",
        settings: Settings | None = None,
    ) -> None:
        self.app = app
        # The absolute ceiling, and the upload allowance. Nothing is ever
        # allowed more than this, whichever rule below applies.
        self.max_bytes = max_bytes
        # The webhook gets its own, much smaller cap. It is the one endpoint
        # that is unauthenticated, unlimited by policy (ADR-032) and reachable
        # by anybody who finds the URL, and a WhatsApp delivery is a few
        # kilobytes of JSON - so the general 32 MB allowance, which exists for
        # media uploads by signed-in colleagues, is three orders of magnitude
        # more than it can legitimately need. Signature verification happens
        # after the body is read, so without this the cost of making the server
        # buffer 32 MB is one unsigned request.
        self.webhook_max_bytes = webhook_max_bytes or max_bytes
        # Left unset, every tier collapses to `max_bytes` - the single-cap
        # behaviour a directly constructed middleware has always had.
        self.json_max_bytes = json_max_bytes or max_bytes
        self.authenticated_max_bytes = authenticated_max_bytes or self.json_max_bytes
        self.document_max_bytes = document_max_bytes or self.authenticated_max_bytes
        self.api_prefix = api_prefix.rstrip("/")
        # Only to verify an access token when a body would exceed the public
        # cap. Without settings nothing is ever treated as signed in.
        self.settings = settings

    def _cap_for(self, path: str) -> int:
        """The cap an *anonymous* caller gets on this path.

        For a webhook, the smaller of the two caps, never the larger. `min`
        rather than a straight substitution, and an existing test caught why:
        an operator who lowers `MAX_REQUEST_BYTES` below the webhook's own
        setting would otherwise have *raised* the limit on the one endpoint an
        unauthenticated caller can reach. A cap named for one endpoint should
        only ever tighten the general one.

        `_elevated_cap` decides whether a signed-in caller may send more.
        """
        if path.startswith(WEBHOOK_PREFIX):
            return min(self.webhook_max_bytes, self.max_bytes)
        return min(self.json_max_bytes, self.max_bytes)

    def _allowance_for(self, method: str, path: str) -> int:
        """The cap a *signed-in* caller gets on this route."""
        if path.startswith(self.api_prefix + "/"):
            below = path[len(self.api_prefix) :]
            for allowance in UPLOAD_ALLOWANCES:
                if allowance.method == method and allowance.path.fullmatch(below):
                    chosen = self.max_bytes if allowance.cap == UPLOAD else self.document_max_bytes
                    return min(chosen, self.max_bytes)
        return min(self.authenticated_max_bytes, self.max_bytes)

    def _authenticated(self, headers: Headers) -> bool:
        """Whether the request carries an access token that verifies.

        Capacity, not authorization - see the module docstring. Any failure to
        verify is simply "not signed in": the request keeps the public cap and
        the route answers it however it would have.
        """
        if self.settings is None:
            return False
        value = headers.get("authorization")
        if not value or value[: len(_BEARER)].lower() != _BEARER:
            return False
        # Imported here rather than at the top: `app.core.security` is heavier
        # than this module and is only needed for a body over the public cap.
        from app.core.security import TokenType, decode_token

        try:
            decode_token(
                value[len(_BEARER) :].strip(),
                settings=self.settings,
                expected_type=TokenType.ACCESS,
            )
        except AuthenticationError:
            return False
        return True

    def _elevated_cap(self, scope: Scope, headers: Headers, public: int) -> int:
        path = scope.get("path", "")
        if path.startswith(WEBHOOK_PREFIX):
            return public
        allowance = self._allowance_for(scope.get("method", "GET"), path)
        if allowance <= public or not self._authenticated(headers):
            return public
        return allowance

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        cap = self._cap_for(scope.get("path", ""))
        declared = headers.get("content-length")
        known = int(declared) if declared is not None and declared.isdigit() else None
        # Only a body that would exceed the public cap - or a streamed one whose
        # length is not declared - costs a token check. A request with neither
        # header has no body at all, and an ordinary one pays nothing.
        streamed = known is None and "transfer-encoding" in headers
        if streamed or (known is not None and known > cap):
            cap = self._elevated_cap(scope, headers, cap)
        if known is not None and known > cap:
            # Refused without reading a byte of it.
            logger.warning(
                "request.body_too_large",
                extra={
                    "event": "request.body_too_large",
                    "path": scope.get("path", ""),
                    "declared_bytes": known,
                },
            )
            await self._refuse_oversize(send)
            return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    # A request that lied about its length, or sent none at all.
                    # Counted as it streams and cut off at the same cap.
                    exceeded = True
                    logger.warning(
                        "request.body_too_large",
                        extra={
                            "event": "request.body_too_large",
                            "path": scope.get("path", ""),
                            "received_bytes": received,
                        },
                    )
                    return {"type": "http.disconnect"}
            return message

        started = False
        replaced = False

        async def guarded_send(message: Message) -> None:
            # Once the stream has been cut off, whatever the application makes
            # of the disconnect - FastAPI answers 400 "error parsing the body" -
            # is replaced by the answer that is true: the body was too large. A
            # response that had already started is left alone; rewriting half
            # of one would corrupt it.
            nonlocal started, replaced
            if message["type"] == "http.response.start":
                if exceeded and not started:
                    started = replaced = True
                    await self._refuse_oversize(send)
                    return
                started = True
            if not replaced:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)
        if exceeded and not started:
            # The application said nothing at all about the disconnect.
            await self._refuse_oversize(send)

    async def _refuse_oversize(self, send: Send) -> None:
        await _refuse(
            send,
            status_code=413,
            code="payload_too_large",
            message="The request body is larger than this server accepts.",
        )


class RequestTimeoutMiddleware:
    """Bounds how long a handler may take.

    A handler waiting on something for two minutes is holding a pooled database
    connection while it waits, so the timeout protects a shared resource rather
    than the client's patience.

    504 rather than 500: the request was not malformed and the server is not
    broken - something downstream took too long, and that is worth telling a
    caller accurately so a retry is a sensible thing for them to do.
    """

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        started = False

        async def watched_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.app(scope, receive, watched_send)
        except TimeoutError:
            logger.error(
                "request.timed_out",
                extra={
                    "event": "request.timed_out",
                    "path": scope.get("path", ""),
                    "timeout_seconds": self.timeout_seconds,
                },
            )
            if started:
                # The response is already going out; there is nothing to say
                # that would not corrupt it.
                raise
            await _refuse(
                send,
                status_code=504,
                code="request_timeout",
                message="The request took too long to complete.",
            )

    def _exempt(self, path: str) -> bool:
        """Whether this path must be allowed to finish however long it takes.

        Only the webhook: a timed-out delivery is a non-2xx, Meta retries it,
        and a subscription that keeps failing is eventually disabled (ADR-032).
        """
        return path.startswith(TIMEOUT_EXEMPT_PREFIXES)
