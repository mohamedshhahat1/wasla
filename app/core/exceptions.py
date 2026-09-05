"""Domain exceptions and centralised HTTP error handling.

Services and repositories raise domain exceptions; the API layer never builds
error responses by hand. Responses use a stable envelope and never expose
stack traces or internal details in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, cast

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Settings
from app.core.logging import current_log_context, get_logger
from app.core.telemetry import observe_unhandled_error

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
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.details = details
        self.error_code = error_code or type(self).error_code
        self.status_code = status_code or type(self).status_code
        # Response headers that belong to the failure itself rather than to the
        # request - `Retry-After` on a refusal, and nothing else so far. Kept
        # on the exception because the handler is the only place that builds a
        # response, and a caller told to back off without being told for how
        # long will simply retry immediately.
        self.headers = headers
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


class PlanLimitExceededError(WaslaError):
    """The workspace's plan does not allow this, and no role change would.

    402 rather than 403, which is not pedantry. A permission error tells a
    caller to ask an administrator; this one tells them to upgrade, and a
    client that cannot tell the two apart shows the wrong dialogue to a
    customer who is trying to give us money.
    """

    status_code = 402
    error_code = "plan_limit_exceeded"
    message = "This workspace's plan does not allow that."


class PaymentRequiredError(WaslaError):
    """The action needs money that has not been collected yet.

    402, like `PlanLimitExceededError`, and deliberately a different `code`.
    Both tell a caller to reach for a card rather than for an administrator,
    but they are different sentences: one says the current plan does not
    stretch that far, and this one says the plan being asked for has not been
    paid for. A client that rendered them alike would tell somebody to upgrade
    when they are already trying to.
    """

    status_code = 402
    error_code = "payment_required"
    message = "That plan has not been paid for."


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
    402: "plan_limit_exceeded",
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


# Keys pydantic puts in a validation error that must never reach a response.
# `input` is the submitted value itself - the password, the invitation token,
# the Meta access token - and `url` is a link to pydantic's documentation for
# the error type, which tells a caller nothing and pins our error shape to
# theirs. `ctx` can carry fragments of the value for some error types.
_UNSAFE_ERROR_KEYS: Final = frozenset({"input", "url", "ctx"})


def _safe_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Describe what was wrong without repeating what was sent.

    Pydantic's `errors()` includes an `input` key holding the offending value
    verbatim. Returning it means a request that fails validation has its own
    payload read back to it - and a 422 body travels: reverse-proxy access
    logs, APM and error-tracking payloads, browser HAR captures, client-side
    reporters. A rejected over-length password was being handed back in full,
    in every environment including production, which contradicts the project's
    own rule that a credential is never logged.

    `loc` and `type` are kept because they are what makes an error actionable -
    which field, and what rule it broke - and neither contains caller data.
    `msg` is pydantic's own English and is kept for the same reason; it
    describes the constraint rather than the value.
    """
    safe: list[dict[str, Any]] = []
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        safe.append({key: value for key, value in entry.items() if key not in _UNSAFE_ERROR_KEYS})
    return cast(list[dict[str, Any]], jsonable_encoder(safe))


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
            headers=error.headers,
        )

    async def handle_request_validation_error(request: Request, exc: Exception) -> Response:
        error = cast(RequestValidationError, exc)
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_error",
                "The request payload is invalid.",
                details={"errors": _safe_validation_errors(error.errors())},
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
        # The one counter that says "something is wrong here that nobody
        # anticipated". Unlabelled deliberately: the interesting breakdown is
        # `wasla_http_requests_total{status="5xx"}` by route, and repeating it
        # here with an exception type would put a class name from any library
        # in the tree into a label domain.
        observe_unhandled_error()
        # The exception class and message go to the log and no further unless
        # the only person who can read the response is the person running the
        # process. An exception raised deep in a driver quotes the thing it
        # could not reach, which is a hostname and sometimes a credential -
        # and `staging` is internet-reachable, so it was publishing both.
        #
        # What the caller gets instead is the request id, which is what finds
        # the logged exception. Suppressing the body without that would trade a
        # disclosure for an unanswerable support ticket.
        details = (
            {"exception": type(exc).__name__, "message": str(exc)}
            if settings.is_developer_environment
            else None
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
