"""The error envelope every failed request answers with, as models (FEB-02, ADR-191).

These describe what `app/core/exceptions.py` already writes - `error_payload`
and the handlers registered beside it - and exist so the OpenAPI document can
say so. They are not used to build a response: the handlers stay the one place
an error body is made, and `tests/integration/test_error_envelope_contract.py`
holds real bodies from real routes to the schemas exported from these.

Optionality mirrors the handlers exactly. `details` is written only when there
is something to say and `request_id` only when the request has one, so both are
optional rather than nullable-and-always-present.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """What went wrong, in a word a client can switch on and a sentence it should not show."""

    model_config = ConfigDict(title="ErrorBody")

    code: str = Field(
        description=(
            "A stable, machine-readable code, such as `validation_error`, "
            "`unauthenticated`, `rate_limited` or `email_verification_required`. "
            "The list grows; a client treats an unknown code as the class of its "
            "HTTP status. See the error codes in docs/API.md."
        ),
    )
    message: str = Field(
        description="An English sentence for logs and error reports, not for the interface.",
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Structured detail, when there is any. Absent otherwise.",
    )
    request_id: str | None = Field(
        default=None,
        description="The request's id, also sent as the X-Request-ID header. Absent when none.",
    )


class ErrorEnvelope(BaseModel):
    """The body of every non-2xx answer: `{"error": {...}}`."""

    model_config = ConfigDict(title="ErrorEnvelope")

    error: ErrorBody


class ValidationErrorItem(BaseModel):
    """One field that failed validation. The submitted value is never echoed back."""

    model_config = ConfigDict(title="ValidationErrorItem")

    type: str = Field(description="The rule that failed, e.g. `missing`, `string_too_short`.")
    loc: list[str | int] = Field(
        description="Where: `body`, `query`, `path` or `header`, then the field path."
    )
    msg: str = Field(description="The constraint, in English. Never contains the value.")


class ValidationErrorDetails(BaseModel):
    """`details` of a 422."""

    model_config = ConfigDict(title="ValidationErrorDetails")

    errors: list[ValidationErrorItem]


class ValidationErrorBody(BaseModel):
    """`error` of a 422: code `validation_error`, with the failing fields."""

    model_config = ConfigDict(title="ValidationErrorBody")

    code: str = Field(description="`validation_error`.")
    message: str = Field(description="An English sentence, not for the interface.")
    details: ValidationErrorDetails | None = Field(
        default=None,
        description="The failing fields.",
    )
    request_id: str | None = Field(
        default=None,
        description="The request's id, also sent as the X-Request-ID header. Absent when none.",
    )


class ValidationErrorEnvelope(BaseModel):
    """The body of a 422: the envelope, with `details.errors` naming each failing field."""

    model_config = ConfigDict(title="ValidationErrorEnvelope")

    error: ValidationErrorBody


ERROR_MODELS: tuple[type[BaseModel], ...] = (
    ErrorBody,
    ErrorEnvelope,
    ValidationErrorItem,
    ValidationErrorDetails,
    ValidationErrorBody,
    ValidationErrorEnvelope,
)

__all__ = [
    "ERROR_MODELS",
    "ErrorBody",
    "ErrorEnvelope",
    "ValidationErrorBody",
    "ValidationErrorDetails",
    "ValidationErrorEnvelope",
    "ValidationErrorItem",
]
