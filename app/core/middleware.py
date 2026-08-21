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
