"""Every request body refuses fields nobody declared.

Three agent schemas (`AgentCreate`, `AgentUpdate`, `ToolGrantRequest`) were
found subclassing `BaseModel` directly while the rest of the package went
through a base that forbids extras, so `POST /agents` with an injected
`"tenant_id": "<another workspace>"` answered 201 instead of 422 (AUTHZ-03).

Nothing crossed a boundary. The agent was created in the caller's own
workspace, because the tenant comes from the signed token by way of
`ActiveWorkspaceDep` and the route copies declared fields one at a time - the
injected value was read by nobody. The finding was that the project's own
defence had a hole in it and the hole was invisible: six existing tests assert
`extra="forbid"` by name on six chosen models, which is exactly the kind of
coverage that says nothing about the seventh.

So this file does not test three schemas. It enumerates every body model
FastAPI actually binds - resolved from the dependency graph, the same way
`test_route_authorization.py` resolves guards - and asks each one the question.
A request model added next year is covered the day it is routed, which is the
only version of this test worth having.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration

# Field names that decide who a request acts as or on whose behalf. A request
# model that quietly accepts one of these is the shape of the finding: the
# value is inert today because the server takes these from context, and the
# next route to read a field off a body would make it not inert.
SENSITIVE_FIELDS = (
    "tenant_id",
    "workspace_id",
    "platform_role",
    "role",
    "is_admin",
    "user_id",
    "owner_id",
    "created_by",
)

# A field name no schema declares, so a model that accepts it accepts anything.
TYPO = "definitely_not_a_declared_field"


def _routes(routes: Sequence[Any]) -> Iterator[APIRoute]:
    """Every `APIRoute`, descending through deferred inclusion."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif isinstance(route, _IncludedRouter):
            yield from _routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _routes(route.routes)


def _models(annotation: Any) -> Iterator[type[BaseModel]]:
    """Pydantic models inside an annotation, unions and containers included."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for argument in getattr(annotation, "__args__", ()) or ():
        yield from _models(argument)


def _is_synthesised(model: type[BaseModel]) -> bool:
    """A model FastAPI built itself for a form or multipart signature.

    `POST /conversations/{id}/messages/media` takes `UploadFile` plus form
    fields, and FastAPI assembles a throwaway model to validate them. It is not
    a schema anybody wrote and there is nothing in `app/schemas` to harden, so
    the rule cannot reach it - and an unknown *form part* is refused by the
    multipart parser regardless. Recognised by where it was built rather than
    by name, so this exemption cannot be borrowed by a model of ours.
    """
    return not model.__module__.startswith("app.")


@pytest.fixture(scope="module")
def body_models() -> dict[type[BaseModel], list[str]]:
    """Every request body model, mapped to the routes that bind it."""
    app = create_app(Settings(_env_file=None, environment="test"))
    bound: dict[type[BaseModel], list[str]] = {}
    for route in _routes(app.routes):
        field = getattr(route, "body_field", None)
        if field is None:
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            for model in _models(field.field_info.annotation):
                bound.setdefault(model, []).append(f"{method} {route.path}")
    return bound


def test_the_inventory_is_not_empty(body_models: dict[type[BaseModel], list[str]]) -> None:
    """The control. A walker that finds nothing would pass every test below.

    This is the failure the whole file is vulnerable to: `_routes` missing
    `_IncludedRouter` returns seven routes out of 138, and an empty inventory
    satisfies every `for model in ...` assertion silently.
    """
    ours = [model for model in body_models if not _is_synthesised(model)]
    assert len(ours) >= 40, f"only {len(ours)} request models found; the walker is missing routes"


def test_every_request_body_forbids_unknown_fields(
    body_models: dict[type[BaseModel], list[str]],
) -> None:
    """Structural: the configuration is set, on every one of them."""
    lenient = {
        f"{model.__module__}.{model.__name__}": sorted(routes)
        for model, routes in body_models.items()
        if not _is_synthesised(model) and model.model_config.get("extra") != "forbid"
    }
    assert not lenient, (
        "these request bodies accept undeclared fields; add "
        f"`model_config = ConfigDict(extra='forbid')`: {lenient}"
    )


@pytest.mark.parametrize("field", [*SENSITIVE_FIELDS, TYPO])
def test_no_request_body_silently_accepts_an_authorization_field(
    body_models: dict[type[BaseModel], list[str]], field: str
) -> None:
    """Behavioural: the configuration actually refuses, field by field.

    Asserted by construction rather than by reading `model_config`, because the
    thing that matters is the refusal and not the setting that is supposed to
    produce it. A model that *declares* the field is skipped - `user_id` on a
    conversation assignment is the request, and the service still proves the
    membership before honouring it.
    """
    accepted = []
    for model, routes in body_models.items():
        if _is_synthesised(model) or field in model.model_fields:
            continue
        try:
            model.model_validate({field: "injected"})
        except ValidationError as error:
            if any(
                detail["type"] == "extra_forbidden" and detail["loc"] == (field,)
                for detail in error.errors()
            ):
                continue
            # Refused for another reason - a required field is missing, which
            # is a refusal too, but not the one under test. Ask the narrow
            # question instead: does the extra field alone raise?
            accepted.append(f"{model.__module__}.{model.__name__} via {sorted(routes)}")
        else:
            accepted.append(f"{model.__module__}.{model.__name__} via {sorted(routes)}")
    assert not accepted, f"{field!r} is accepted as an undeclared field by: {accepted}"
