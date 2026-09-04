"""Every free-form field a caller can write into is bounded by something.

The audit that produced this file counted the string-bearing fields on request
models and found nine with no bound at all. Four of them mattered: two JSONB
columns (`leads.custom_fields`, `agent_tools.config`) and two structures
forwarded to Meta (template components on a send and on a follow-up). An
authenticated workspace member could write megabytes into any of them, limited
only by the 32 MB request cap and 300 requests a minute.

Counting them once fixes nine fields. This asserts the property, so the tenth
cannot be added without a decision.

**What counts as free-form.** Most fields are bounded by being what they are: a
`uuid` is bounded by being a uuid, an enum by its members, a `bool` by having
two values. What is left is plain text, decimals, and JSON whose shape is
deliberately not modelled - and each of those has to say how large it may be.

**What counts as bounded.** A `max_length` or `max_digits` in the field's
metadata, a bound on the items of a list, or a validator registered for the
field - which is how the four JSON fields are bounded, because a budget with
four axes cannot be expressed as a `max_length`.

**Exemptions are data, with reasons**, in the same shape
`test_deployment_configuration.py` uses. A field that belongs here has an entry
somebody had to write, rather than an absence nobody noticed.
"""

from __future__ import annotations

import decimal
import inspect
import sys
import types
import typing
import uuid
from datetime import date, datetime
from typing import Any, Final

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.main import create_app

# Fields that are free-form by the definition above and are deliberately
# unbounded here, each with the reason and where the bound actually lives.
EXEMPT: Final[dict[tuple[str, str], str]] = {
    (
        "Body_send_media_api_v1_conversations__conversation_id__messages_media_post",
        "file",
    ): (
        "A multipart upload rather than a JSON field. Bounded by MEDIA_MAX_BYTES "
        "while the body is read, and by the workspace's storage entitlement "
        "before the object is written."
    ),
}


def _request_component_names() -> dict[str, set[str]]:
    """Component name -> the routes whose body reaches it, from the OpenAPI doc.

    Read from the document rather than from the schema modules because that is
    what the API actually accepts: a model nothing references cannot be posted,
    and a model reached only as a nested field can be.
    """
    app = create_app(Settings(_env_file=None, environment="test", jwt_secret="x" * 40))
    document = app.openapi()
    components = document.get("components", {}).get("schemas", {})
    found: dict[str, set[str]] = {}

    for path, operations in document.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            body = operation.get("requestBody")
            if not body:
                continue
            label = f"{method.upper()} {path}"
            stack: list[Any] = [body]
            seen: set[str] = set()
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    reference = item.get("$ref")
                    if isinstance(reference, str) and reference.startswith("#/components/"):
                        name = reference.rsplit("/", 1)[1]
                        if name not in seen:
                            seen.add(name)
                            found.setdefault(name, set()).add(label)
                            stack.append(components.get(name, {}))
                        continue
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
    return found


def _models_by_name() -> dict[str, type[BaseModel]]:
    """Every Pydantic model this application defines, by class name.

    FastAPI names a component after the class, and that name is the only handle
    the OpenAPI document gives back - so every module the application imported
    is searched rather than just `app.schemas`. A request model declared beside
    its route would otherwise be silently skipped, which is the shape of gap
    this file exists to close.
    """
    models: dict[str, type[BaseModel]] = {}
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("app."):
            continue
        for name, obj in vars(module).items():
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
                models.setdefault(name, obj)
    return models


#: Annotations that bound themselves. A uuid cannot be a megabyte.
SELF_BOUNDING: Final = (uuid.UUID, datetime, date, bool, int, float, type(None))


def _unwrap(annotation: Any) -> list[Any]:
    """Every concrete type an annotation admits, unions and containers flattened."""
    out: list[Any] = []
    stack: list[Any] = [annotation]
    while stack:
        item = stack.pop()
        origin = typing.get_origin(item)
        if origin in (typing.Union, types.UnionType):
            stack.extend(typing.get_args(item))
        elif origin in (list, set, tuple, frozenset):
            out.append(list)
            stack.extend(typing.get_args(item))
        elif origin is dict:
            out.append(dict)
            stack.extend(typing.get_args(item)[1:])
        elif origin is typing.Annotated:
            stack.append(typing.get_args(item)[0])
        else:
            out.append(item)
    return out


def _is_free_form(annotation: Any) -> bool:
    """Whether arbitrary caller content can be written through this annotation."""
    for member in _unwrap(annotation):
        if member is Any or member is str or member is dict:
            return True
        if member is decimal.Decimal:
            return True
        # A `str` subclass that is not an enum is still free text. A StrEnum is
        # its members, and `__members__` is what tells them apart.
        if (
            inspect.isclass(member)
            and issubclass(member, str)
            and not hasattr(member, "__members__")
        ):
            return True
    return False


def _has_constraint(annotation: Any, field: Any) -> bool:
    """A max_length or max_digits anywhere on the field or inside its items.

    Walked rather than read off `field.metadata`, because pydantic puts the
    constraint in two different places depending on the declaration. A required
    `str` carries `MaxLen` directly; an optional one becomes
    `Optional[Annotated[str, FieldInfo(metadata=[MaxLen(...)])]]` and the
    constraint is a level down. A check that only read the first shape would
    pass every required field and fail every optional one, which is the
    opposite of useful.
    """
    stack: list[Any] = [annotation, field]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))

        if getattr(item, "max_length", None) is not None:
            return True
        if getattr(item, "max_digits", None) is not None:
            return True

        metadata = getattr(item, "metadata", None)
        if isinstance(metadata, list | tuple):
            stack.extend(metadata)
        stack.extend(typing.get_args(item))
    return False


def _validated_fields(model: type[BaseModel]) -> set[str]:
    """Fields a validator is registered for.

    The four JSON fields are bounded this way rather than with a `max_length`,
    because their budget has four axes - total bytes, entries per container,
    nesting depth and per-string length - and none of them is a length.
    """
    names: set[str] = set()
    decorators = model.__pydantic_decorators__
    for validator in decorators.field_validators.values():
        names.update(validator.info.fields)
    return names


def _free_form_fields() -> list[tuple[str, str, str]]:
    """(component, field, routes) for every free-form request field."""
    components = _request_component_names()
    models = _models_by_name()
    rows: list[tuple[str, str, str]] = []
    for name, routes in sorted(components.items()):
        model = models.get(name)
        if model is None:
            if name in _ENUM_COMPONENTS:
                # An enum is bounded by its members.
                continue
            # Generated by FastAPI for a multipart body; handled by EXEMPT.
            rows.append((name, "file", ", ".join(sorted(routes))))
            continue
        for field_name, field in model.model_fields.items():
            if not _is_free_form(field.annotation):
                continue
            rows.append((name, field_name, ", ".join(sorted(routes))))
    return rows


def _enum_component_names() -> set[str]:
    """Components the document itself declares as enums."""
    app = create_app(Settings(_env_file=None, environment="test", jwt_secret="x" * 40))
    schemas = app.openapi().get("components", {}).get("schemas", {})
    return {name for name, schema in schemas.items() if schema.get("enum")}


_ENUM_COMPONENTS = _enum_component_names()
FREE_FORM = _free_form_fields()


def test_the_inventory_is_not_empty() -> None:
    """A guard on the guard: a walk that finds nothing proves nothing."""
    assert len(FREE_FORM) > 40, FREE_FORM


@pytest.mark.parametrize(
    ("component", "field", "routes"),
    FREE_FORM,
    ids=[f"{component}.{field}" for component, field, _ in FREE_FORM],
)
def test_every_free_form_request_field_is_bounded(component: str, field: str, routes: str) -> None:
    if (component, field) in EXEMPT:
        return

    models = _models_by_name()
    model = models[component]
    info = model.model_fields[field]

    bounded = _has_constraint(info.annotation, info) or field in _validated_fields(model)

    assert bounded, (
        f"{component}.{field} is reachable from {routes} and carries no bound. "
        f"Give it a max_length sized to its column, a max_digits matching its "
        f"Numeric precision, or a validator using app.schemas.bounds - or add it "
        f"to EXEMPT with the reason and where the bound actually lives."
    )
