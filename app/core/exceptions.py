"""Domain exceptions and centralised HTTP error handling.

Services and repositories raise domain exceptions; the API layer never builds
error responses by hand. Responses use a stable envelope and never expose
stack traces or internal details in production.
"""

from __future__ import annotations

from typing import Any, Final, cast

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.logging import current_log_context, get_logger

logger = get_logger(__name__)


class WaslaError(Exception):
    """Base class for expected application failures."""

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        error_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.details = details
        self.error_code = error_code or type(self).error_code
        self.status_code = status_code or type(self).status_code
        super().__init__(self.message)


class ValidationError(WaslaError):
    status_code = 422
    error_code = "validation_error"
    message = "The request payload is invalid."


class AuthenticationError(WaslaError):
    status_code = 401
    error_code = "unauthenticated"
    message = "Authentication is required."


class PermissionDeniedError(WaslaError):
    status_code = 403
    error_code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(WaslaError):
    status_code = 404
    error_code = "not_found"
    message = "The requested resource was not found."


class TenantIsolationError(NotFoundError):
    """Raised on cross-tenant access.

    Deliberately surfaced as a 404 so callers cannot use error codes to probe
    for the existence of another tenant's resources. The real reason is logged.
    """


class ConflictError(WaslaError):
    status_code = 409
    error_code = "conflict"
    message = "The request conflicts with the current state."


class RateLimitedError(WaslaError):
    status_code = 429
    error_code = "rate_limited"
    message = "Too many requests."


class ExternalServiceError(WaslaError):
    status_code = 502
    error_code = "external_service_error"
    message = "An upstream service failed."


class DependencyUnavailableError(WaslaError):
    status_code = 503
    error_code = "dependency_unavailable"
    message = "A required dependency is unavailable."


_HTTP_ERROR_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "dependency_unavailable",
}


def _request_id(request: Request) -> str | None:
    from_state = getattr(request.state, "request_id", None)
    if isinstance(from_state, str):
        return from_state
    return current_log_context().get("request_id")


def error_payload(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the stable error envelope returned by every failed request."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    if request_id:
        error["request_id"] = request_id
    return {"error": error}


def register_exception_handlers(app: FastAPI) -> None:
    """Map exceptions to consistent HTTP responses."""
    settings = cast(Settings, app.state.settings)

    async def handle_wasla_error(request: Request, exc: Exception) -> Response:
        error = cast(WaslaError, exc)
        log = logger.warning if error.status_code < 500 else logger.error
        log(
            "request.error",
            extra={
                "event": "request.error",
                "error_code": error.error_code,
                "status_code": error.status_code,
                "method": request.method,
                "path": request.url.path,
                "reason": type(error).__name__,
            },
        )
        return JSONResponse(
            status_code=error.status_code,
            content=error_payload(
                error.error_code,
                error.message,
                details=error.details,
                request_id=_request_id(request),
            ),
        )

    async def handle_request_validation_error(request: Request, exc: Exception) -> Response:
        error = cast(RequestValidationError, exc)
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_error",
                "The request payload is invalid.",
                details={"errors": jsonable_encoder(error.errors())},
                request_id=_request_id(request),
            ),
        )

    async def handle_http_exception(request: Request, exc: Exception) -> Response:
        error = cast(StarletteHTTPException, exc)
        code = _HTTP_ERROR_CODES.get(error.status_code, f"http_{error.status_code}")
        message = str(error.detail) if error.detail else "The request could not be completed."
        return JSONResponse(
            status_code=error.status_code,
            content=error_payload(code, message, request_id=_request_id(request)),
            headers=getattr(error, "headers", None),
        )

    async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
        logger.exception(
            "request.unhandled_error",
            extra={
                "event": "request.unhandled_error",
                "method": request.method,
                "path": request.url.path,
            },
        )
        details = (
            None
            if settings.is_production
            else {"exception": type(exc).__name__, "message": str(exc)}
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "internal_error",
                "An unexpected error occurred.",
                details=details,
                request_id=_request_id(request),
            ),
        )

    app.add_exception_handler(WaslaError, handle_wasla_error)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
