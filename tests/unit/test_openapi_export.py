"""The exported OpenAPI document: fresh, complete, and honest about errors (FEB-02, ADR-191).

The frontend generates its typed client from the committed
`frontend/packages/api-client/openapi.json`, because production serves no
document. These tests are what keep that file the truth:

- **fresh** - the committed bytes equal what the code exports now, so a change
  to a route that forgets to regenerate the file fails here, in the backend's
  CI, before the frontend ever sees a drifted client;
- **complete** - its operations are exactly the routes a production-shaped
  application serves, less the ones that exclude themselves from the schema;
- **honest** - every operation's errors reference the envelope the handlers
  really write (`tests/integration/test_error_envelope_contract.py` checks the
  bodies themselves).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from app.api.openapi import (
    ERROR_REF,
    VALIDATION_ERROR_REF,
    declare_error_envelope,
    install_error_envelope,
)
from app.core.config import Settings
from app.core.events import EVENT_TYPES
from app.main import create_app
from scripts import export_openapi

METHODS = frozenset({"get", "put", "post", "delete", "patch"})
#: Routes that keep themselves out of the schema, each on purpose.
SELF_EXCLUDED = frozenset({("GET", "/metrics")})


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    return export_openapi.build_files()


@pytest.fixture(scope="module")
def document(files: dict[str, str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(files[export_openapi.OPENAPI_FILE])
    return loaded


def _operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


# ----------------------------------------------------------------- OA-02 fresh


def test_the_committed_document_is_what_the_code_exports(files: dict[str, str]) -> None:
    stale = [
        name
        for name, text in files.items()
        if not (export_openapi.OUTPUT_DIR / name).exists()
        or (export_openapi.OUTPUT_DIR / name).read_bytes() != text.encode("utf-8")
    ]

    assert not stale, f"{', '.join(stale)} out of date: {export_openapi.REGENERATE}"


def test_the_check_mode_agrees() -> None:
    assert export_openapi.stale_files(export_openapi.OUTPUT_DIR) == []


def test_the_export_is_deterministic(files: dict[str, str]) -> None:
    again = export_openapi.build_files()

    assert again == files
    for text in files.values():
        assert text.endswith("}\n")
        assert "\r" not in text


def test_check_mode_writes_nothing_and_names_the_fix(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert export_openapi.main(["--check", "--output-dir", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []
    assert export_openapi.REGENERATE in capsys.readouterr().err

    assert export_openapi.main(["--output-dir", str(tmp_path)]) == 0
    assert export_openapi.main(["--check", "--output-dir", str(tmp_path)]) == 0


# -------------------------------------------------------------- OA-01 complete


def _walk(router: Any, found: list[APIRoute]) -> list[APIRoute]:
    """Every route, through the nested routers FastAPI keeps (the plan's walk, section 4)."""
    for route in getattr(router, "routes", []):
        nested = getattr(route, "original_router", None)
        if nested is not None:
            _walk(nested, found)
        elif isinstance(route, APIRoute):
            found.append(route)
        else:
            _walk(route, found)
    return found


def _full_path(route: APIRoute) -> str:
    # The walk sees each router's own paths; the API's are mounted under its prefix.
    if route.path.startswith(("/health", "/metrics")):
        return route.path
    return "/api/v1" + route.path


def test_the_operations_are_the_routes_production_serves(document: dict[str, Any]) -> None:
    production = create_app(Settings(_env_file=None, environment="test", docs_enabled=False))
    routes = _walk(production.router, [])
    served = {(method, _full_path(route)) for route in routes for method in route.methods or ()}
    excluded = {
        (method, _full_path(route))
        for route in routes
        if not route.include_in_schema
        for method in route.methods or ()
    }
    documented = {(method.upper(), path) for path, method, _ in _operations(document)}

    assert excluded == SELF_EXCLUDED
    assert documented == served - excluded
    assert len(served) == 259


def test_production_still_serves_no_document() -> None:
    production = create_app(Settings(_env_file=None, environment="test", docs_enabled=False))

    assert production.openapi_url is None
    paths = {getattr(route, "path", None) for route in production.routes}
    assert not paths & {"/openapi.json", "/docs", "/redoc"}


# ---------------------------------------------------------------- OA-04 honest


def test_every_operation_has_the_envelope_as_its_default(document: dict[str, Any]) -> None:
    missing = [
        (path, method)
        for path, method, operation in _operations(document)
        if operation["responses"].get("default", {}).get("content", {}).get("application/json", {})
        != {"schema": {"$ref": ERROR_REF}}
    ]

    assert not missing
    assert len(_operations(document)) > 200


def test_every_declared_error_references_the_envelope(document: dict[str, Any]) -> None:
    wrong = []
    for path, method, operation in _operations(document):
        for status, response in operation["responses"].items():
            if status[:1] not in {"4", "5"}:
                continue
            expected = VALIDATION_ERROR_REF if status == "422" else ERROR_REF
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if schema != {"$ref": expected}:
                wrong.append((path, method, status, schema))

    assert not wrong


def test_fastapis_own_error_shape_is_gone(files: dict[str, str], document: dict[str, Any]) -> None:
    text = files[export_openapi.OPENAPI_FILE]

    assert "HTTPValidationError" not in text
    assert '#/components/schemas/ValidationError"' not in text
    assert "ValidationError" not in document["components"]["schemas"]
    for name in ("ErrorEnvelope", "ErrorBody", "ValidationErrorEnvelope", "ValidationErrorItem"):
        assert name in document["components"]["schemas"]


def test_the_envelope_names_the_fields_the_handlers_write(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]

    assert schemas["ErrorEnvelope"]["required"] == ["error"]
    assert set(schemas["ErrorBody"]["properties"]) == {"code", "message", "details", "request_id"}
    assert sorted(schemas["ErrorBody"]["required"]) == ["code", "message"]
    assert set(schemas["ValidationErrorItem"]["properties"]) == {"type", "loc", "msg"}


def _raw_document() -> dict[str, Any]:
    """What FastAPI generates on its own, before the envelope is declared."""
    application = create_app(Settings(_env_file=None, environment="test", docs_enabled=True))
    raw: dict[str, Any] = get_openapi(
        title=application.title, version=application.version, routes=application.routes
    )
    return raw


def test_a_422_is_declared_only_where_fastapi_declared_one(document: dict[str, Any]) -> None:
    raw = _raw_document()

    for path, method, operation in _operations(document):
        before = raw["paths"][path][method]["responses"]
        assert ("422" in operation["responses"]) == ("422" in before), (path, method)
        assert set(operation["responses"]) - {"default"} == set(before), (path, method)


def test_nothing_but_the_error_responses_changed(document: dict[str, Any]) -> None:
    raw = _raw_document()

    for path, method, operation in _operations(document):
        before = dict(raw["paths"][path][method])
        after = dict(operation)
        before_responses, after_responses = before.pop("responses"), after.pop("responses")
        assert before == after, (path, method)
        for status, response in before_responses.items():
            if status[:1] == "2":
                assert after_responses[status] == response, (path, method, status)
            else:
                assert after_responses[status]["description"] == response["description"]


def test_declaring_the_envelope_twice_changes_nothing(document: dict[str, Any]) -> None:
    again = declare_error_envelope(json.loads(json.dumps(document)))

    assert again == document


def test_the_post_processor_is_installed_on_the_application() -> None:
    application = create_app(Settings(_env_file=None, environment="test", docs_enabled=True))

    first = application.openapi()
    assert first["paths"]["/api/v1/auth/login"]["post"]["responses"]["default"]
    assert application.openapi() is first


def test_installing_it_on_a_bare_application_declares_the_envelope() -> None:
    application = FastAPI()

    @application.get("/thing/{thing_id}")
    async def thing(thing_id: int) -> dict[str, int]:
        return {"id": thing_id}

    install_error_envelope(application)
    responses = application.openapi()["paths"]["/thing/{thing_id}"]["get"]["responses"]

    assert responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": VALIDATION_ERROR_REF
    }
    assert responses["default"]["content"]["application/json"]["schema"] == {"$ref": ERROR_REF}


# ------------------------------------------------------------- OA-06 the stream


def test_the_event_vocabulary_is_the_codes(files: dict[str, str]) -> None:
    exported = json.loads(files[export_openapi.EVENT_TYPES_FILE])

    assert exported["event_types"] == sorted(EVENT_TYPES)
    assert "membership_revoked" in exported["close_reasons"]
    assert "workspace_switched" in exported["close_reasons"]
    assert exported["stream_frames"] == ["closed", "resync"]
