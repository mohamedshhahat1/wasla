"""The request-body caps fit every legitimate request, and no more (SEC-02).

A cap that is too large is the amplification the audit measured; a cap that is
too small is an outage for whichever customer first sends a long agent prompt
in Arabic. Neither is visible until it happens, so this file derives the
largest body each route can *legitimately* receive from the live OpenAPI
schema and asserts it fits the cap that route will actually be given.

"Largest" is pessimistic on purpose: every string at its `maxLength` with every
character sent as a six-byte `\\uXXXX` escape, every array at `maxItems`, and a
free-shaped object at its `JsonBounds` byte budget escaped the same way. A
client that sends raw UTF-8 - which is what browsers do - uses a third of that.

When a schema grows past its cap this fails and names the route. The fix is a
deliberate decision - a bigger tier, or an entry in `UPLOAD_ALLOWANCES` - rather
than a customer's 413.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

import pytest

from app.core.config import Settings
from app.core.limits import DOCUMENT, UPLOAD, UPLOAD_ALLOWANCES
from app.main import create_app
from app.schemas.bounds import LEAD_CUSTOM_FIELDS, TEMPLATE_COMPONENTS, TOOL_CONFIG

ESCAPED = 6  # bytes a character costs when sent as \uXXXX
# The free-shaped fields, and the `JsonBounds` each is checked against *as a
# whole* - a components list is one budget, not a budget per component. The
# budget is in decoded UTF-8 bytes, which escaping can at most triple.
FREE_FIELDS = {
    "custom_fields": LEAD_CUSTOM_FIELDS,
    "config": TOOL_CONFIG,
    "components": TEMPLATE_COMPONENTS,
    "template_components": TEMPLATE_COMPONENTS,
}
# The one string field with no declared length: a multipart file part, whose
# size is what `UPLOAD_ALLOWANCES` exists for.
UNSIZED_FILE = "file"


@cache
def _spec() -> dict[str, Any]:
    settings = Settings(_env_file=None, environment="test", docs_enabled=True)
    spec: dict[str, Any] = create_app(settings).openapi()
    return spec


def _worst(schema: dict[str, Any], defs: dict[str, Any], field: str = "") -> int:
    if field in FREE_FIELDS and "properties" not in schema:
        return FREE_FIELDS[field].max_bytes * 3
    if "$ref" in schema:
        return _worst(defs[schema["$ref"].rsplit("/", 1)[-1]], defs, field)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return max(_worst(option, defs, field) for option in schema[key])
    if "allOf" in schema:
        return sum(_worst(part, defs, field) for part in schema["allOf"])
    if "enum" in schema:
        return max(len(json.dumps(value)) for value in schema["enum"])
    kind = schema.get("type")
    if kind == "string":
        if "maxLength" in schema:
            return 2 + int(schema["maxLength"]) * ESCAPED
        if schema.get("format") in ("uuid", "date-time", "date"):
            return 40
        if field == UNSIZED_FILE:
            return 0  # measured by the upload allowance, not here
        # A decimal amount or similar pattern-constrained scalar.
        return 2 + 64 * ESCAPED
    if kind in ("integer", "number"):
        return 32
    if kind == "boolean":
        return 5
    if kind == "null":
        return 4
    if kind == "array":
        items = schema.get("maxItems")
        item = _worst(schema.get("items", {}), defs, field)
        if items is None:
            # Unbounded arrays of enum members (lead statuses) are bounded by
            # the cap itself; count a generous number of them.
            items = 100
        return 2 + items * (1 + item)
    properties = schema.get("properties", {})
    if properties:
        return 2 + sum(
            len(name) + 4 + _worst(value, defs, name) for name, value in properties.items()
        )
    # A free shape nobody declared a bound for. Refusing to guess is the
    # point: a new unbounded field must be given one before it ships.
    raise AssertionError(f"{field or 'a body'} has a free shape and no known bound")


def _operations() -> list[tuple[str, str, int, bool]]:
    spec = _spec()
    defs = spec.get("components", {}).get("schemas", {})
    found = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            if not body:
                continue
            worst = max(_worst(media["schema"], defs) for media in body["content"].values())
            found.append((method.upper(), path, worst, bool(operation.get("security"))))
    return found


def _below_prefix(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path.removeprefix("/api/v1"))


def _allowance(method: str, path: str) -> str | None:
    for allowance in UPLOAD_ALLOWANCES:
        if allowance.method == method and allowance.path.fullmatch(_below_prefix(path)):
            return allowance.cap
    return None


DEFAULTS = Settings(_env_file=None, environment="test")
CAPS = {
    None: DEFAULTS.max_authenticated_request_bytes,
    DOCUMENT: DEFAULTS.max_document_request_bytes,
    UPLOAD: DEFAULTS.max_request_bytes,
}


def test_the_schema_walk_found_the_routes_it_is_about() -> None:
    """Non-vacuity: a walk that found nothing would pass everything below."""
    paths = {path for _, path, _, _ in _operations()}

    assert "/api/v1/auth/login" in paths
    assert "/api/v1/agents" in paths
    assert "/api/v1/knowledge/bases/{knowledge_base_id}/documents" in paths


@pytest.mark.parametrize(
    ("method", "path", "worst", "secured"),
    _operations(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_legitimate_body_fits_the_cap_its_route_gets(
    method: str, path: str, worst: int, secured: bool
) -> None:
    cap = CAPS[_allowance(method, path)]

    assert worst <= cap, f"{method} {path}: a legitimate body can reach {worst} bytes, cap {cap}"


@pytest.mark.parametrize(
    ("method", "path", "worst"),
    [(m, p, w) for m, p, w, secured in _operations() if not secured],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_public_body_fits_the_unauthenticated_cap(method: str, path: str, worst: int) -> None:
    """The routes an anonymous caller can reach take nothing near 64 KiB."""
    assert worst <= DEFAULTS.max_json_request_bytes, f"{method} {path}: {worst} bytes"


def test_the_public_routes_include_the_credential_endpoints() -> None:
    public = {path for _, path, _, secured in _operations() if not secured}

    assert {"/api/v1/auth/login", "/api/v1/auth/register"} <= public


@pytest.mark.parametrize("allowance", UPLOAD_ALLOWANCES, ids=lambda a: a.path.pattern)
def test_every_upload_allowance_names_a_route_that_exists(allowance: Any) -> None:
    """A stale entry would be a large allowance waiting for a route to reuse it."""
    matches = [
        path
        for method, path, _, _ in _operations()
        if method == allowance.method and allowance.path.fullmatch(_below_prefix(path))
    ]

    assert len(matches) == 1


def test_every_upload_allowance_is_on_an_authenticated_route() -> None:
    for method, path, _, secured in _operations():
        if _allowance(method, path) is not None:
            assert secured, f"{method} {path} has a large allowance and no authentication"


def test_every_multipart_route_has_an_explicit_allowance() -> None:
    spec = _spec()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            if "multipart/form-data" in content:
                assert _allowance(method.upper(), path) == UPLOAD, f"{method} {path}"


def test_the_tiers_are_ordered_by_default() -> None:
    assert (
        DEFAULTS.max_json_request_bytes
        < DEFAULTS.max_authenticated_request_bytes
        < DEFAULTS.max_document_request_bytes
        < DEFAULTS.max_request_bytes
    )
    assert DEFAULTS.max_json_request_bytes <= 64 * 1024
    assert DEFAULTS.webhook_max_request_bytes == 1024 * 1024
