"""HTTP middleware."""

from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.logging import bind_log_context, clear_log_context, get_logger

logger = get_logger(__name__)

# Probe traffic is logged at DEBUG so it cannot drown out real requests.
_QUIET_PATHS = frozenset({"/health", "/health/live", "/health/ready"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, binds the log context, and emits one access log."""

    def __init__(self, app: ASGIApp, *, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(self.header_name) or uuid4().hex
        request.state.request_id = request_id
        bind_log_context(request_id=request_id)
        started = perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed",
                extra={
                    "event": "request.failed",
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            raise
        else:
            response.headers[self.header_name] = request_id
            logger.log(
                logging.DEBUG if request.url.path in _QUIET_PATHS else logging.INFO,
                "request.completed",
                extra={
                    "event": "request.completed",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            return response
        finally:
            clear_log_context()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Sets the response headers a browser needs to defend the caller.

    These exist in `nginx/nginx.conf` as well, and that is not duplication worth
    removing: nginx is one deployment topology rather than a property of the
    software. `docker-compose.prod.yml` runs the API as its own container and
    the image can be run directly, and in both of those every header configured
    in the proxy is simply absent. A control that only exists in a file the
    application does not ship with is a control you cannot rely on.

    What each one is actually for, since a list of headers copied between
    projects is how the wrong ones end up shipping:

    - **`X-Content-Type-Options: nosniff`** stops a browser re-typing a
      response by inspecting its bytes. This API serves customer-uploaded media
      through `/conversations/{id}/media/{id}`, so content sniffing is the
      difference between an uploaded file being downloaded and being executed
      on this origin.
    - **`X-Frame-Options: DENY`** and **`frame-ancestors 'none'`** keep the API
      out of an iframe. There is no interface here to clickjack today, but the
      interactive docs are reachable in non-production environments.
    - **`Referrer-Policy: no-referrer`** stops a URL - which for this API can
      contain conversation, lead and media identifiers - being handed to
      whatever a customer's browser navigates to next.
    - **`Content-Security-Policy`** is deliberately restrictive: this is a JSON
      API, so `default-src 'none'` is honest, and the only reason
      `img-src`/`style-src` are permitted is the Swagger UI that is served when
      `docs_enabled` is on.
    - **`Cache-Control: no-store`** on API responses, because they carry
      workspace data and access tokens and neither belongs in a shared cache or
      on disk in a browser profile.

    **HSTS is set only when the request arrived over HTTPS.** Sending it over
    plain HTTP is meaningless - a browser ignores it - and sending it from a
    local development server would pin `localhost` to HTTPS in that developer's
    browser for a year, which is a genuinely unpleasant thing to do to somebody.
    The forwarded protocol is read only from a peer we already trust for
    forwarding headers; see `app/api/rate_limits.py` for why that matters.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts_seconds: int = 31_536_000,
        trusted_proxies: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(app)
        self._hsts_seconds = hsts_seconds
        self._trusted_proxies = trusted_proxies

    def _is_https(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else None
        if peer is not None and peer in self._trusted_proxies:
            # Only believed from a proxy we listed. Otherwise any caller could
            # assert HTTPS and collect an HSTS pin for this host.
            return request.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"
        return False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers

        # `setdefault` throughout: a handler that has already made a deliberate
        # choice - the media route sets its own `Content-Disposition` and
        # `nosniff` - keeps it.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
            "img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
        )
        headers.setdefault("Cache-Control", "no-store")

        if self._is_https(request):
            headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self._hsts_seconds}; includeSubDomains",
            )
        return response
