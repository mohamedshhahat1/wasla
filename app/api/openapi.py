"""The OpenAPI document tells the truth about errors (FEB-02, ADR-191).

FastAPI declares a 422 on every operation that validates input, with its own
`HTTPValidationError` - `{"detail": [...]}` - while this API has always answered
`{"error": {"code": "validation_error", ...}}` (`app/core/exceptions.py`). And it
declares nothing at all for 401, 403, 404, 409 or 429. A client generated from
that document types every error wrong.

One post-processor, installed on the application's `openapi()`, fixes it for
every operation at once rather than router by router - a new router cannot
forget it, and `tests/unit/test_openapi_export.py` proves it covers every
operation:

- an existing `422` references `ValidationErrorEnvelope` (none is added where
  FastAPI declared none);
- every other declared 4xx/5xx references `ErrorEnvelope`, keeping its description;
- every operation gains `default` -> `ErrorEnvelope`;
- FastAPI's `HTTPValidationError` and `ValidationError` leave `components` once
  nothing references them.

Nothing else changes: no path, parameter, request body, 2xx response or
operationId. Production still serves no document (`DOCS_ENABLED`); the
frontend's copy is exported by `scripts/export_openapi.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from pydantic.json_schema import models_json_schema

from app.schemas.errors import ERROR_MODELS, ErrorEnvelope, ValidationErrorEnvelope

REF_TEMPLATE = "#/components/schemas/{model}"
ERROR_REF = REF_TEMPLATE.format(model=ErrorEnvelope.__name__)
VALIDATION_ERROR_REF = REF_TEMPLATE.format(model=ValidationErrorEnvelope.__name__)
DEFAULT_DESCRIPTION = "Error envelope (see docs/API.md)"
FASTAPI_ERROR_COMPONENTS = ("HTTPValidationError", "ValidationError")
_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _json_body(ref: str) -> dict[str, Any]:
    return {"application/json": {"schema": {"$ref": ref}}}


def error_components() -> dict[str, Any]:
    """The envelope's JSON Schemas, named as components."""
    _, definitions = models_json_schema(
        [(model, "serialization") for model in ERROR_MODELS], ref_template=REF_TEMPLATE
    )
    components: dict[str, Any] = definitions["$defs"]
    return components


def declare_error_envelope(document: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a generated document in place so every error references the envelope.

    Idempotent: a document that already went through it comes out unchanged.
    """
    schemas: dict[str, Any] = document.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in error_components().items():
        existing = schemas.get(name)
        if existing is not None and existing != schema:
            raise ValueError(f"an API schema is already named {name!r}")
        schemas[name] = schema

    for path_item in document.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in _METHODS:
                continue
            responses: dict[str, Any] = operation.setdefault("responses", {})
            for status, response in responses.items():
                if status == "422":
                    response["content"] = _json_body(VALIDATION_ERROR_REF)
                elif status[:1] in {"4", "5"}:
                    response["content"] = _json_body(ERROR_REF)
            responses["default"] = {
                "description": DEFAULT_DESCRIPTION,
                "content": _json_body(ERROR_REF),
            }

    for name in FASTAPI_ERROR_COMPONENTS:
        if name not in schemas:
            continue
        others = {key: value for key, value in schemas.items() if key != name}
        rest = json.dumps({"paths": document.get("paths", {}), "schemas": others})
        if f'"{REF_TEMPLATE.format(model=name)}"' not in rest:
            del schemas[name]
    return document


def install_error_envelope(app: FastAPI) -> None:
    """Make `app.openapi()` return the declared-envelope document, cached as FastAPI caches it."""
    generate: Callable[[], dict[str, Any]] = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None and getattr(app.state, "error_envelope_declared", False):
            return app.openapi_schema
        document = declare_error_envelope(generate())
        app.openapi_schema = document
        app.state.error_envelope_declared = True
        return document

    # FastAPI documents replacing `app.openapi` as the way to customise the
    # schema; mypy sees a method being assigned and cannot know that is intended.
    app.openapi = openapi  # type: ignore[method-assign]


__all__ = [
    "DEFAULT_DESCRIPTION",
    "ERROR_REF",
    "VALIDATION_ERROR_REF",
    "declare_error_envelope",
    "error_components",
    "install_error_envelope",
]
