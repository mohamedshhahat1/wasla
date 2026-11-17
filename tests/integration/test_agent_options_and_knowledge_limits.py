"""What the AI and knowledge screens learn from the API (FEB-37, FEB-38, FEB-39).

`GET /agents/options` and `GET /knowledge/limits` are read from the same
objects the refusals use, so a screen can never offer a model the API would
refuse or send a file it would turn away. `X-Next-Cursor` is exposed through
CORS so the knowledge lists' next page is readable from the app's origin.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.api.dependencies import ActiveWorkspace, get_active_workspace, get_entitlement_service
from app.api.v1 import agents as agents_routes
from app.api.v1 import knowledge as knowledge_routes
from app.api.v1.knowledge import NEXT_CURSOR_HEADER
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.knowledge import DocumentSource
from app.main import create_app
from app.schemas import agent as agent_schema
from app.schemas.agent import AgentCreate, AgentUpdate
from app.schemas.knowledge import MAX_DOCUMENT_TITLE_LENGTH
from app.services import document_formats, knowledge_limits
from app.services.agent_service import AgentService
from tests.conftest import AllowingEntitlements, FakeDependency
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
WEB_ORIGIN = "https://app.wasla.test"

AppFactory = Callable[..., FastAPI]


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(id=uuid.uuid4(), user_id=USER_ID, tenant_id=TENANT_ID, role=role),
        tenant=Tenant(
            timezone=TEST_TIMEZONE,
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


@pytest.fixture
def make_app(
    settings: Settings, fake_database: FakeDependency, fake_redis: FakeDependency
) -> AppFactory:
    def build(role: TenantRole | None = TenantRole.MEMBER, **overrides: Any) -> FastAPI:
        application = create_app(settings.model_copy(update=overrides))
        application.state.database = fake_database
        application.state.redis = fake_redis
        application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
        if role is not None:
            application.dependency_overrides[get_active_workspace] = lambda: _workspace(role)
        return application

    return build


async def _get(application: FastAPI, path: str, **headers: str) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://wasla.test"
    ) as http:
        return await http.get(path, headers=headers)


# ------------------------------------------------------------------ options


EVERY_ROLE = [TenantRole.MEMBER, TenantRole.TENANT_ADMIN, TenantRole.TENANT_OWNER]


@pytest.mark.parametrize("role", EVERY_ROLE)
async def test_every_member_reads_the_same_options(make_app: AppFactory, role: TenantRole) -> None:
    """AO-T1."""
    response = await _get(make_app(role), "/api/v1/agents/options")
    assert response.status_code == 200
    member = await _get(make_app(TenantRole.MEMBER), "/api/v1/agents/options")
    assert response.json() == member.json()


async def test_options_need_a_signed_in_member(make_app: AppFactory) -> None:
    """AO-T1: no workspace dependency override → the real guard answers 401."""
    response = await _get(make_app(role=None), "/api/v1/agents/options")
    assert response.status_code == 401


async def test_the_model_list_is_the_default_then_the_allow_list(make_app: AppFactory) -> None:
    """AO-T2: the default first, then the allow-list in its order, no duplicate."""
    body = (
        await _get(
            make_app(
                openai_model="model-b",
                openai_allowed_models=["model-a", "model-b", "model-c", "model-a"],
            ),
            "/api/v1/agents/options",
        )
    ).json()
    assert body["models"] == ["model-b", "model-a", "model-c"]
    assert body["default_model"] == "model-b"


async def test_an_unrestricted_deployment_answers_null(make_app: AppFactory) -> None:
    """AO-T2."""
    body = (await _get(make_app(openai_allowed_models=[]), "/api/v1/agents/options")).json()
    assert body["models"] is None


def _service(settings: Settings) -> AgentService:
    return AgentService(session=None, settings=settings, tenant_id=TENANT_ID)  # type: ignore[arg-type]


async def test_the_ceiling_in_the_read_is_the_one_the_service_enforces(
    make_app: AppFactory, settings: Settings
) -> None:
    """AO-T3: the read and the rule agree - one resolver."""
    tuned = settings.model_copy(update={"openai_max_output_tokens": 3_000})
    body = (await _get(make_app(openai_max_output_tokens=3_000), "/api/v1/agents/options")).json()
    assert body["max_output_tokens"] == {"default": 3_000, "ceiling": 3_000}
    service = _service(tuned)
    assert service._checked_output_tokens(body["max_output_tokens"]["ceiling"]) == 3_000
    assert service._checked_output_tokens(None) == body["max_output_tokens"]["default"]
    with pytest.raises(ValidationError) as refused:
        service._checked_output_tokens(body["max_output_tokens"]["ceiling"] + 1)
    assert refused.value.error_code == "agent_output_tokens_exceed_ceiling"


_JSON_NAMES = {"max_length": "maxLength", "ge": "minimum", "le": "maximum"}


def _constraint(model: type[BaseModel], field: str, name: str) -> Any:
    """A field's bound as the model publishes it (optional fields sit in `anyOf`)."""
    schema = model.model_json_schema()["properties"][field]
    for candidate in [schema, *schema.get("anyOf", [])]:
        if _JSON_NAMES[name] in candidate:
            return candidate[_JSON_NAMES[name]]
    raise AssertionError(f"{model.__name__}.{field} has no {name}")


async def test_every_bound_in_the_read_is_the_schemas_own(make_app: AppFactory) -> None:
    """AO-T4: read from the pydantic models, not retyped."""
    bounds = (await _get(make_app(), "/api/v1/agents/options")).json()["bounds"]
    assert bounds["name"]["max_length"] == _constraint(AgentCreate, "name", "max_length")
    assert bounds["description"]["max_length"] == _constraint(
        AgentUpdate, "description", "max_length"
    )
    assert bounds["system_prompt"]["max_length"] == _constraint(
        AgentCreate, "system_prompt", "max_length"
    )
    for field in ("temperature", "memory_message_limit", "memory_token_budget"):
        assert bounds[field]["min"] == _constraint(AgentCreate, field, "ge"), field
        assert bounds[field]["max"] == _constraint(AgentCreate, field, "le"), field


async def test_a_model_in_the_read_is_accepted_and_one_outside_is_refused(
    make_app: AppFactory, settings: Settings
) -> None:
    """AO-T5."""
    overrides = {"openai_model": "model-a", "openai_allowed_models": ["model-b"]}
    models = (await _get(make_app(**overrides), "/api/v1/agents/options")).json()["models"]
    service = _service(settings.model_copy(update=overrides))
    for model in models:
        assert service._checked_model(model) == model
    with pytest.raises(ValidationError) as refused:
        service._checked_model("model-z")
    assert refused.value.error_code == "agent_model_not_permitted"
    assert refused.value.status_code == 422
    assert refused.value.message == (
        "The model model-z is not available on this deployment. Choose one of: model-a, model-b."
    )


async def test_the_options_carry_names_and_numbers_only(make_app: AppFactory) -> None:
    """AO-T6: no key, no provider address, nothing of a workspace."""
    application = make_app(openai_allowed_models=["model-x"])
    body = (await _get(application, "/api/v1/agents/options")).json()
    assert set(body) == {
        "models",
        "default_model",
        "max_output_tokens",
        "bounds",
        "default_knowledge_grant",
    }
    assert set(body["bounds"]) == {
        "name",
        "description",
        "system_prompt",
        "temperature",
        "memory_message_limit",
        "memory_token_budget",
    }
    flat = repr(body).lower()
    for forbidden in ("key", "http", "url", "sk-", str(TENANT_ID)):
        assert forbidden not in flat, forbidden


def _route_index(router: APIRouter, path: str, method: str = "GET") -> int:
    for index, route in enumerate(router.routes):
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or ()):
            return index
    raise AssertionError(f"no {method} {path}")


async def test_options_is_declared_before_the_agent_identifier(make_app: AppFactory) -> None:
    """AO-T7: `/options` can never reach `read_agent` as an id."""
    application = make_app()
    assert _route_index(agents_routes.router, "/agents/options") < _route_index(
        agents_routes.router, "/agents/{agent_id}"
    )
    response = await _get(application, "/api/v1/agents/options")
    assert response.status_code == 200
    assert "models" in response.json()


async def test_default_knowledge_grant_follows_the_setting(make_app: AppFactory) -> None:
    for value in (True, False):
        body = (
            await _get(make_app(agent_default_knowledge_grant=value), "/api/v1/agents/options")
        ).json()
        assert body["default_knowledge_grant"] is value


def test_the_message_limit_bound_is_a_module_constant() -> None:
    assert agent_schema.MIN_MESSAGE_LIMIT == 1
    assert agent_schema.MAX_MESSAGE_LIMIT == 200


# ------------------------------------------------------------------- limits


async def test_the_limits_are_the_checks_own_constants(make_app: AppFactory) -> None:
    """KL-T1: per source, the same function the upload's check calls."""
    body = (await _get(make_app(), "/api/v1/knowledge/limits")).json()
    by_source = {row["source"]: row for row in body["sources"]}
    assert set(by_source) == {source.value for source in DocumentSource}
    for source in DocumentSource:
        assert by_source[source.value]["max_bytes"] == document_formats.max_upload_bytes(source)
        assert by_source[source.value]["media_types"] == [document_formats.MEDIA_TYPES[source]]
    assert by_source["pdf"]["max_bytes"] == knowledge_limits.MAX_BINARY_DOCUMENT_BYTES == 300_000
    assert by_source["text"]["max_bytes"] == document_formats.MAX_TEXT_UPLOAD_BYTES == 1_600_000
    assert body["max_pdf_pages"] == knowledge_limits.MAX_PDF_PAGES
    assert body["max_characters"] == knowledge_limits.MAX_SUBMITTED_CHARACTERS
    assert body["max_title_length"] == MAX_DOCUMENT_TITLE_LENGTH == 300


async def test_a_moved_constant_moves_the_read_and_the_check_together(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KL-T1: patch the constant, see both move."""
    monkeypatch.setattr(document_formats, "MAX_BINARY_DOCUMENT_BYTES", 1_000)
    body = (await _get(make_app(), "/api/v1/knowledge/limits")).json()
    pdf = next(row for row in body["sources"] if row["source"] == "pdf")
    assert pdf["max_bytes"] == 1_000
    with pytest.raises(document_formats.UploadTooLargeError):
        document_formats.upload_as_submission(b"%PDF" + b"0" * 997, source=DocumentSource.PDF)


async def test_the_extensions_are_the_uploads_own(make_app: AppFactory) -> None:
    """KL-T2: every accepted extension, and nothing else."""
    body = (await _get(make_app(), "/api/v1/knowledge/limits")).json()
    read = {ext: row["source"] for row in body["sources"] for ext in row["extensions"]}
    expected = {
        ".txt": "text",
        ".md": "markdown",
        ".markdown": "markdown",
        ".pdf": "pdf",
        ".csv": "csv",
        ".xlsx": "xlsx",
        ".docx": "docx",
        ".html": "html",
        ".htm": "html",
    }
    assert read == expected
    for extension, source in expected.items():
        assert document_formats.upload_source(None, f"file{extension}").value == source


@pytest.mark.parametrize("source", list(DocumentSource))
def test_an_upload_at_the_limit_passes_the_size_check_and_one_byte_more_does_not(
    source: DocumentSource,
) -> None:
    """KL-T3: the existing refusal, unchanged code."""
    limit = document_formats.max_upload_bytes(source)
    refusal: BaseException | None = None
    try:
        document_formats.upload_as_submission(b"a" * limit, source=source)
    except Exception as error:  # a format refusal is fine; a size refusal is not
        refusal = error
    assert not isinstance(refusal, document_formats.UploadTooLargeError), source
    with pytest.raises(document_formats.UploadTooLargeError) as refused:
        document_formats.upload_as_submission(b"a" * (limit + 1), source=source)
    assert refused.value.error_code == "document_too_large"
    assert refused.value.status_code == 413


async def test_the_limits_carry_nothing_of_a_workspace(make_app: AppFactory) -> None:
    """KL-T4."""
    body = (await _get(make_app(), "/api/v1/knowledge/limits")).json()
    assert set(body) == {
        "sources",
        "max_request_bytes",
        "max_pdf_pages",
        "max_characters",
        "max_title_length",
    }
    for row in body["sources"]:
        assert set(row) == {"source", "extensions", "media_types", "max_bytes"}
    assert str(TENANT_ID) not in repr(body)
    assert body["max_request_bytes"] == 5 * 1024 * 1024


@pytest.mark.parametrize("role", EVERY_ROLE)
async def test_every_member_reads_the_limits(make_app: AppFactory, role: TenantRole) -> None:
    """KL-T5."""
    assert (await _get(make_app(role), "/api/v1/knowledge/limits")).status_code == 200


async def test_the_limits_need_a_signed_in_member(make_app: AppFactory) -> None:
    assert (await _get(make_app(role=None), "/api/v1/knowledge/limits")).status_code == 401


async def test_limits_is_a_static_path(make_app: AppFactory) -> None:
    """KL-T6: no `/{…}` route under /knowledge could take `limits` as an id."""
    application = make_app()
    limits = _route_index(knowledge_routes.router, "/knowledge/limits")
    dynamic = [
        index
        for index, route in enumerate(knowledge_routes.router.routes)
        if isinstance(route, APIRoute) and route.path.count("/") == 2 and "{" in route.path
    ]
    assert all(limits < index for index in dynamic)
    response = await _get(application, "/api/v1/knowledge/limits")
    assert response.status_code == 200 and "sources" in response.json()


# ------------------------------------------------------------------- cursor


async def test_an_allowed_origin_can_read_the_next_cursor(make_app: AppFactory) -> None:
    """KC-T1."""
    response = await _get(make_app(cors_origins=[WEB_ORIGIN]), "/health/live", Origin=WEB_ORIGIN)
    exposed = {
        value.strip().lower()
        for value in response.headers.get("access-control-expose-headers", "").split(",")
    }
    assert exposed == {"x-request-id", NEXT_CURSOR_HEADER.lower()}
    assert response.headers.get("access-control-allow-origin") == WEB_ORIGIN


async def test_an_origin_not_allowed_gets_no_cors_headers(make_app: AppFactory) -> None:
    """KC-T3: the allowed origins did not widen."""
    response = await _get(
        make_app(cors_origins=[WEB_ORIGIN]), "/health/live", Origin="https://evil.example"
    )
    # Starlette still lists the exposed headers, but without an allow-origin
    # the browser lets the page read nothing, the cursor included.
    assert "access-control-allow-origin" not in response.headers
