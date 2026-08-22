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

The WhatsApp webhook is exempt from the timeout for the same reason it is exempt
from rate limiting (ADR-032): a timed-out webhook is a non-2xx, Meta retries it,
and eventually the subscription is disabled. It keeps the body limit, because a
body cap protects memory rather than shedding load.
"""

from __future__ import annotations

import asyncio
from typing import Final

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import error_payload
from app.core.logging import get_logger

logger = get_logger(__name__)

# Paths that must never be cut off mid-flight. Prefix-matched, so the
# verification handshake and the delivery endpoint are both covered.
TIMEOUT_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/api/v1/webhooks",)


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

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            # Refused without reading a byte of it.
            logger.warning(
                "request.body_too_large",
                extra={
                    "event": "request.body_too_large",
                    "path": scope.get("path", ""),
                    "declared_bytes": int(declared),
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
                if received > self.max_bytes:
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

        await self.app(scope, limited_receive, send)
        if exceeded:  # pragma: no cover - the disconnect ends the response first
            return

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
