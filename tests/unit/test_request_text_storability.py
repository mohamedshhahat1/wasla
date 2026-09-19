"""Every free-text request field refuses what PostgreSQL cannot hold (SEC-04).

The audit found four fields that still let a NUL through to the database after
the CRM and knowledge ones had been closed - each closed one at a time, each
found by somebody probing. This file turns the rule around so the next field is
safe by default: it walks **every** request body model of **every** route in
the live application and asserts that each string field refuses `a\\x00b` at
validation, field and model validators both.

The only exceptions are listed below with a reason, and they share one: the
value is a secret that is hashed or compared and never stored, logged or sent
anywhere as text. A new field that is neither refused nor listed fails here.

Query parameters are not request bodies and are pinned separately below.
"""

from __future__ import annotations

from functools import cache
from typing import Any, get_args

import pytest
from fastapi.routing import _IncludedRouter
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.api.v1.leads import SearchQuery as LeadSearch
from app.api.v1.leads import TagQuery
from app.api.v1.platform import SearchQuery as PlatformSearch
from app.core.config import Settings
from app.main import create_app

NUL = "a" + chr(0) + "b"

# Secrets that are only ever hashed or compared. A NUL in one is a wrong
# secret, and a wrong secret is already refused - there is nothing to store.
NEVER_STORED: dict[str, str] = {
    "AccountDeleteRequest.current_password": "verified against an Argon2 hash",
    "AccountDeleteRequest.reauthentication_token": "compared against a stored digest",
    "GoogleCallbackRequest.code": "form-encoded to Google's token endpoint, never stored",
    "GoogleCallbackRequest.state": "a Redis key lookup, never stored",
    "InvitationAcceptRequest.token": "hashed before lookup",
    "LoginRequest.password": "verified against an Argon2 hash",
    "LogoutRequest.refresh_token": "a signed JWT, decoded not stored",
    "PasswordChangeRequest.current_password": "verified against an Argon2 hash",
    "PasswordResetConfirmPayload.new_password": "hashed with Argon2 before storage",
    "RefreshRequest.refresh_token": "a signed JWT, decoded not stored",
    "VerificationConfirmRequest.code": "normalised to digits and hashed",
    "WorkspaceDeleteRequest.confirmation": "compared in Python with the slug",
}


def _string_like(annotation: Any) -> bool:
    if annotation is str:
        return True
    return any(_string_like(a) for a in get_args(annotation) if a is not type(None))


@cache
def _models() -> tuple[type[BaseModel], ...]:
    app = create_app(Settings(_env_file=None, environment="test"))

    def flatten(items: Any) -> Any:
        for item in items:
            if isinstance(item, _IncludedRouter):
                yield from flatten(item.effective_candidates())
            else:
                yield item

    found: set[type[BaseModel]] = set()
    for route in flatten(app.router.routes):
        body = getattr(route, "body_field", None)
        if body is None:
            continue
        model = getattr(body, "type_", None) or body.field_info.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            found.add(model)
    return tuple(sorted(found, key=lambda m: m.__name__))


def _fields() -> list[tuple[type[BaseModel], str]]:
    return [
        (model, name)
        for model in _models()
        for name, field in model.model_fields.items()
        if _string_like(field.annotation)
    ]


def _refuses_nul(model: type[BaseModel], field: str) -> bool:
    """Whether assigning a NUL to this one field fails *on this field*."""
    try:
        model.__pydantic_validator__.validate_assignment(model.model_construct(), field, NUL)
    except ValidationError as error:
        return any(detail["loc"][:1] == (field,) for detail in error.errors())
    return False


def test_the_sweep_sees_the_application() -> None:
    """Non-vacuity: dozens of models, including the ones SEC-04 was about."""
    names = {model.__name__ for model in _models()}

    assert len(names) > 40
    assert {"WorkspaceCreateRequest", "WorkspaceUpdateRequest", "SendTextRequest"} <= names


@pytest.mark.parametrize(
    ("model", "field"),
    [(m, f) for m, f in _fields() if f"{m.__name__}.{f}" not in NEVER_STORED],
    ids=lambda value: value.__name__ if isinstance(value, type) else value,
)
def test_every_request_string_field_refuses_nul(model: type[BaseModel], field: str) -> None:
    key = f"{model.__name__}.{field}"
    assert _refuses_nul(model, field), f"{key} accepts a NUL that PostgreSQL cannot hold"


def test_the_exceptions_are_still_real_fields() -> None:
    """A stale exception would silently cover a field of the same name later."""
    present = {f"{model.__name__}.{field}" for model, field in _fields()}

    assert set(NEVER_STORED) <= present


@pytest.mark.parametrize(
    ("name", "adapter"),
    [
        ("leads search", TypeAdapter(LeadSearch)),
        ("platform search", TypeAdapter(PlatformSearch)),
    ],
)
def test_free_text_search_parameters_refuse_nul_and_keep_text(name: str, adapter: Any) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(NUL)
    for text in ("محمد", "🙂", "' OR 1=1-- %_", "tab\there"):
        assert adapter.validate_python(text) == text


def test_the_lead_tag_filter_refuses_nul_in_any_tag() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(TagQuery)

    with pytest.raises(ValidationError):
        adapter.validate_python(["ok", NUL])
    assert adapter.validate_python(["عميل", "vip"]) == ["عميل", "vip"]
