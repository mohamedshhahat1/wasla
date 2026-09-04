"""Bounds for the request fields whose shape is not a fixed model.

Most fields on a request body are bounded by being what they are: a `uuid` is
bounded by being a uuid, an enum by its members, a `str` by the `max_length`
beside it. Four are not, and they are the ones a caller can put an arbitrary
document into - two JSONB columns (`leads.custom_fields`, `agent_tools.config`)
and two structures forwarded to Meta (template components on a send and on a
follow-up). Their shape is deliberately not modelled: it belongs to the tool, or
to a template Meta approved, and modelling it here would put a second copy of
somebody else's contract in this repository.

Not modelled is not the same as not bounded. Without one of these, the only
thing standing between an authenticated workspace member and a multi-megabyte
row is the 32 MB request cap and 300 requests per minute - which is roughly
nine gigabytes an hour of database that nobody is billed for. That is
same-tenant cost abuse rather than a security boundary, and it is still a
liability the platform absorbs.

**Four budgets, not one length.** A single `max_length` on the serialised form
would let `{"a":{"a":{"a":...}}}` through at a few hundred bytes and hand the
recursion to whatever reads it later. So the walk below bounds the total size,
the number of entries in any one object or array, how deep the structure goes,
and how long any single string is - and it is **iterative**, with an explicit
stack, precisely because the value being judged may be pathologically nested and
a recursive validator would meet Python's own recursion limit before it met the
budget.

**The size is accumulated as it walks**, so a value that busts the budget is
refused without ever being serialised. What is counted is what compact UTF-8
JSON would cost, computed one node at a time; it is never smaller than the real
thing, which is the direction a bound has to err in.

**Nothing here names the value it refused.** The messages describe the
constraint, never the content: a validation error's `msg` is the one part
`_safe_validation_errors` keeps, and a bound that echoed the payload back would
be a worse leak than the size it was preventing (ADR-091).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# Per-node overhead in compact JSON: the braces or brackets around a container,
# the quotes around a string, the separator after an entry. Counted so the
# accumulated total is never below what `json.dumps` would produce.
_QUOTES: Final = 2
_BRACKETS: Final = 2
_SEPARATOR: Final = 2


@dataclass(frozen=True, slots=True)
class JsonBounds:
    """What a free-shaped JSON value may cost.

    Every field is a hard refusal rather than a truncation. Truncating a
    customer-supplied structure silently changes what a tool reads or what Meta
    is sent, and a caller who asked for something the system will not do should
    be told so.
    """

    #: Total compact UTF-8 JSON bytes.
    max_bytes: int
    #: Entries in any one object or array. Bounds fan-out.
    max_entries: int
    #: How many containers may be open at once. Bounds nesting.
    max_depth: int
    #: Characters in any one string, key or value.
    max_string: int


#: `leads.custom_fields`: a workspace's own extra columns on a lead. Roomy
#: enough for a CRM's worth of custom attributes, and nowhere near a document.
LEAD_CUSTOM_FIELDS: Final = JsonBounds(
    max_bytes=16_384,
    max_entries=100,
    max_depth=6,
    max_string=4_096,
)

#: `agent_tools.config`: per-grant settings, such as which lead statuses a tool
#: may write. Small by nature - it is configuration, not content.
TOOL_CONFIG: Final = JsonBounds(
    max_bytes=8_192,
    max_entries=50,
    max_depth=6,
    max_string=2_048,
)

#: Template components, forwarded to Meta on a send or a follow-up.
#:
#: Meta's own limits are the reason this is small: a template's whole text -
#: header, body, footer and buttons together - is capped at 1,024 characters,
#: a header or footer at 60, a button title at 20, and there may be at most ten
#: buttons. So a legitimate components array is a handful of objects carrying
#: about a kilobyte of text between them. Eight kilobytes is several times
#: that, which leaves room for parameter scaffolding and media URLs without
#: leaving room for a document.
#:
#: This is a bound on a request body, not a statement about templates - the
#: same distinction `MAX_TEMPLATE_VARIABLES` already draws. Meta remains the
#: authority on what it accepts; what this guarantees is that nothing large
#: reaches the database or the provider in order to find out.
TEMPLATE_COMPONENTS: Final = JsonBounds(
    max_bytes=8_192,
    max_entries=20,
    max_depth=6,
    max_string=2_048,
)


def _scalar_cost(value: Any) -> int:
    if isinstance(value, str):
        return _QUOTES + len(value.encode("utf-8"))
    if value is None:
        return 4
    if isinstance(value, bool):
        return 5
    return len(repr(value))


def check_json(value: Any, bounds: JsonBounds, *, field: str) -> None:
    """Refuse a value that would cost more than `bounds` allows.

    Raises `ValueError`, so pydantic renders it as an ordinary 422 for this
    field rather than a 500 - and `_safe_validation_errors` strips the
    submitted value before the response is written.

    `field` names the field for the message. It is a literal from the schema,
    never caller input.
    """
    total = 0
    # (node, depth). An explicit stack rather than recursion: the value may be
    # nested thousands deep, and a `RecursionError` from a validator is a 500
    # for a request that should have been a 422.
    stack: list[tuple[Any, int]] = [(value, 1)]

    while stack:
        node, depth = stack.pop()
        if depth > bounds.max_depth:
            raise ValueError(f"{field} is nested more than {bounds.max_depth} levels deep.")

        if isinstance(node, dict):
            if len(node) > bounds.max_entries:
                raise ValueError(f"{field} has more than {bounds.max_entries} keys in one object.")
            total += _BRACKETS
            for key, item in node.items():
                if not isinstance(key, str):
                    # Cannot happen through JSON, and would silently change
                    # meaning if it ever did.
                    raise ValueError(f"{field} must use text keys.")
                if len(key) > bounds.max_string:
                    raise ValueError(
                        f"{field} has a key longer than {bounds.max_string} characters."
                    )
                total += _QUOTES + len(key.encode("utf-8")) + _SEPARATOR
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            if len(node) > bounds.max_entries:
                raise ValueError(f"{field} has more than {bounds.max_entries} items in one array.")
            total += _BRACKETS
            for item in node:
                total += _SEPARATOR
                stack.append((item, depth + 1))
        else:
            if isinstance(node, str) and len(node) > bounds.max_string:
                raise ValueError(
                    f"{field} contains a value longer than {bounds.max_string} characters."
                )
            total += _scalar_cost(node)

        # Checked inside the loop rather than after it: a value that has
        # already busted the budget is refused without walking the rest of it.
        if total > bounds.max_bytes:
            raise ValueError(f"{field} is larger than {bounds.max_bytes} bytes.")


__all__ = [
    "LEAD_CUSTOM_FIELDS",
    "TEMPLATE_COMPONENTS",
    "TOOL_CONFIG",
    "JsonBounds",
    "check_json",
]
