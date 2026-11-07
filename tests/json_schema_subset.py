"""Validate a JSON value against the subset of JSON Schema the API's error schemas use.

`jsonschema` is not a dependency of this repository and the locks do not change
for one contract test (FE-0), so this covers exactly the keywords FastAPI and
Pydantic emit for the error envelope - and **refuses** any other keyword rather
than ignoring it, so a schema it cannot judge fails loudly instead of passing.

`strict=True` treats an object schema that lists properties as closed, which is
what makes "the declared schema describes this body" a question with an answer:
FastAPI's `HTTPValidationError` declares no required field and allows anything,
so every object is valid against it in the lenient sense.
"""

from __future__ import annotations

from typing import Any

_ANNOTATIONS = frozenset(
    {"title", "description", "default", "format", "examples", "deprecated", "readOnly"}
)
_ASSERTIONS = frozenset(
    {
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "enum",
        "const",
    }
)
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "object": (dict,),
    "array": (list,),
    "boolean": (bool,),
    "null": (type(None),),
    "integer": (int,),
    "number": (int, float),
}


def _is(value: Any, kind: str) -> bool:
    if isinstance(value, bool) and kind in {"integer", "number"}:
        return False
    return isinstance(value, _TYPES[kind])


def schema_errors(
    value: Any,
    schema: dict[str, Any],
    components: dict[str, Any],
    *,
    strict: bool = False,
    where: str = "$",
) -> list[str]:
    """Every way `value` fails `schema`; empty when it is valid."""
    if "$ref" in schema:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        return schema_errors(value, components[name], components, strict=strict, where=where)
    unknown = set(schema) - _ANNOTATIONS - _ASSERTIONS
    if unknown:
        raise ValueError(f"unsupported JSON Schema keywords at {where}: {sorted(unknown)}")

    declared = schema.get("type")
    if declared is not None:
        kinds = declared if isinstance(declared, list) else [declared]
        if not any(_is(value, kind) for kind in kinds):
            return [f"{where}: expected {declared}, got {type(value).__name__}"]

    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r} is not {schema['const']!r}")
    for branch in schema.get("allOf", []):
        errors += schema_errors(value, branch, components, strict=strict, where=where)
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            passing = [
                branch
                for branch in schema[keyword]
                if not schema_errors(value, branch, components, strict=strict, where=where)
            ]
            if not passing or (keyword == "oneOf" and len(passing) != 1):
                errors.append(f"{where}: no {keyword} branch matches")

    if isinstance(value, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required {name!r}")
        closed = strict and "properties" in schema
        extra = schema.get("additionalProperties", not closed)
        for name, item in value.items():
            if name in properties:
                errors += schema_errors(
                    item, properties[name], components, strict=strict, where=f"{where}.{name}"
                )
            elif extra is False:
                errors.append(f"{where}: undeclared property {name!r}")
            elif isinstance(extra, dict):
                errors += schema_errors(
                    item, extra, components, strict=strict, where=f"{where}.{name}"
                )
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(
                item, schema["items"], components, strict=strict, where=f"{where}[{index}]"
            )
    return errors
