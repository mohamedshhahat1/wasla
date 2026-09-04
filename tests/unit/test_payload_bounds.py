"""What the four free-shaped fields refuse, and what the refusal says.

`test_request_field_bounds.py` asserts that every free-form field carries *a*
bound. This asserts what those bounds actually do - the four budgets, the
pathological shapes each one exists for, and the rule that a refusal never
repeats what it refused.

Driven against the request models rather than over HTTP, because that is where
the decision is made: a value rejected here never reaches a route, a service, a
database or a provider.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.agent import ToolGrantRequest
from app.schemas.bounds import (
    LEAD_CUSTOM_FIELDS,
    TEMPLATE_COMPONENTS,
    TOOL_CONFIG,
    JsonBounds,
    check_json,
)
from app.schemas.campaign import MAX_CAMPAIGN_DESCRIPTION_LENGTH, CampaignCreateRequest
from app.schemas.conversation import SendTemplateRequest
from app.schemas.follow_up import FollowUpCreateRequest
from app.schemas.lead import LeadCreateRequest, LeadUpdateRequest

# A canary long enough to be unmistakable in an error message, and distinctive
# enough that a substring search cannot be fooled.
CANARY = "wsl04-canary-value-that-must-not-be-echoed"

BOUNDS = pytest.mark.parametrize(
    "bounds",
    [LEAD_CUSTOM_FIELDS, TOOL_CONFIG, TEMPLATE_COMPONENTS],
    ids=["lead-custom-fields", "tool-config", "template-components"],
)


def nested(depth: int, *, leaf: Any = 1) -> Any:
    """`{"a": {"a": {... : leaf}}}`, built iteratively.

    Built with a loop rather than recursion for the same reason the validator
    walks with a stack: a test that hit `RecursionError` while constructing its
    own input would be testing Python.
    """
    value: Any = leaf
    for _ in range(depth):
        value = {"a": value}
    return value


# ------------------------------------------------------------- the budgets


@BOUNDS
def test_a_value_within_every_budget_is_accepted(bounds: JsonBounds) -> None:
    check_json({"note": "x" * 64, "count": 3, "on": True, "off": None}, bounds, field="f")


@BOUNDS
def test_a_value_over_the_size_budget_is_refused(bounds: JsonBounds) -> None:
    payload = {f"k{index}": "x" * 100 for index in range(bounds.max_entries)}
    # Deliberately built out of legal parts: every key is short, every string is
    # short, nothing is deeply nested, and the total is still too much.
    while len(json.dumps(payload, separators=(",", ":"))) <= bounds.max_bytes:
        payload = {key: value * 4 for key, value in payload.items()}

    with pytest.raises(ValueError, match="larger than"):
        check_json(payload, bounds, field="f")


@BOUNDS
def test_a_value_with_too_many_entries_is_refused(bounds: JsonBounds) -> None:
    with pytest.raises(ValueError, match="keys in one object"):
        check_json({str(index): 0 for index in range(bounds.max_entries + 1)}, bounds, field="f")

    with pytest.raises(ValueError, match="items in one array"):
        check_json([0] * (bounds.max_entries + 1), bounds, field="f")


@BOUNDS
def test_a_pathologically_nested_value_is_refused(bounds: JsonBounds) -> None:
    """The shape a single `max_length` would let straight through.

    Ten thousand levels of `{"a":` is about 60 kB of JSON and would pass any
    plausible byte budget on its own. What it costs is not size - it is
    whatever walks it next.
    """
    with pytest.raises(ValueError, match="nested more than"):
        check_json(nested(10_000), bounds, field="f")


@BOUNDS
def test_the_nesting_check_does_not_recurse(bounds: JsonBounds) -> None:
    """A depth that would overflow the interpreter is still a 422, not a 500."""
    deep = nested(200_000)

    with pytest.raises(ValueError):
        check_json(deep, bounds, field="f")


@BOUNDS
def test_one_very_long_string_is_refused(bounds: JsonBounds) -> None:
    with pytest.raises(ValueError, match="longer than"):
        check_json({"note": "x" * (bounds.max_string + 1)}, bounds, field="f")


@BOUNDS
def test_one_very_long_key_is_refused(bounds: JsonBounds) -> None:
    with pytest.raises(ValueError, match="key longer than"):
        check_json({"k" * (bounds.max_string + 1): 1}, bounds, field="f")


@BOUNDS
def test_the_size_is_counted_in_bytes_not_characters(bounds: JsonBounds) -> None:
    """Arabic costs two bytes a character, and the store counts bytes.

    A budget measured in characters would be twice as large for exactly the
    workspaces this product is built for.
    """
    # Half the byte budget in characters, which is the whole of it in bytes.
    arabic = "ع" * (bounds.max_bytes // 2)
    generous = JsonBounds(
        max_bytes=bounds.max_bytes,
        max_entries=bounds.max_entries,
        max_depth=bounds.max_depth,
        max_string=len(arabic) + 1,
    )

    with pytest.raises(ValueError, match="larger than"):
        check_json({"n": arabic}, generous, field="f")


# ------------------------------------------------------------ the fields


def _lead(**overrides: Any) -> dict[str, Any]:
    return {"name": "Ahmed", **overrides}


def test_oversized_lead_custom_fields_are_rejected() -> None:
    payload = {f"k{index}": "x" * 1000 for index in range(100)}

    with pytest.raises(ValidationError) as create:
        LeadCreateRequest(**_lead(custom_fields=payload))
    with pytest.raises(ValidationError) as update:
        LeadUpdateRequest(custom_fields=payload)

    for error in (create, update):
        assert error.value.error_count() == 1
        assert error.value.errors()[0]["loc"] == ("custom_fields",)


def test_pathologically_nested_lead_custom_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="nested more than"):
        LeadCreateRequest(**_lead(custom_fields=nested(5_000)))


def test_a_lead_budget_cannot_carry_a_million_digits() -> None:
    """The column is `Numeric(14, 2)`; the schema now says so too."""
    with pytest.raises(ValidationError):
        LeadCreateRequest(**_lead(budget_amount="9" * 1_000_000))


def test_a_reasonable_lead_is_still_accepted() -> None:
    lead = LeadCreateRequest(
        **_lead(
            budget_amount="500000.00",
            tags=["finishing", "cairo"],
            custom_fields={"area_m2": 150, "referred_by": "a neighbour"},
        )
    )

    assert lead.custom_fields == {"area_m2": 150, "referred_by": "a neighbour"}


def test_oversized_agent_tool_config_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        ToolGrantRequest(name="search_knowledge", config={"k": "x" * 100_000})

    assert error.value.errors()[0]["loc"] == ("config",)


def test_oversized_campaign_description_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        CampaignCreateRequest(
            account_id="11111111-1111-1111-1111-111111111111",
            template_id="22222222-2222-2222-2222-222222222222",
            name="Ramadan",
            description="x" * (MAX_CAMPAIGN_DESCRIPTION_LENGTH + 1),
        )

    assert error.value.errors()[0]["loc"] == ("description",)


def test_a_campaign_description_at_the_bound_is_accepted() -> None:
    campaign = CampaignCreateRequest(
        account_id="11111111-1111-1111-1111-111111111111",
        template_id="22222222-2222-2222-2222-222222222222",
        name="Ramadan",
        description="x" * MAX_CAMPAIGN_DESCRIPTION_LENGTH,
    )

    assert campaign.description is not None


@pytest.mark.parametrize(
    ("model", "field", "extra"),
    [
        pytest.param(
            SendTemplateRequest,
            "components",
            {"name": "order_update", "language": "ar"},
            id="send",
        ),
        pytest.param(
            FollowUpCreateRequest,
            "template_components",
            {
                "conversation_id": "33333333-3333-3333-3333-333333333333",
                "delay_minutes": 30,
                "template_name": "order_update",
                "template_language": "ar",
            },
            id="follow-up",
        ),
    ],
)
def test_an_oversized_template_payload_is_rejected_before_meta(
    model: Any, field: str, extra: dict[str, Any]
) -> None:
    """Refused at the schema, so nothing is written and nothing is sent.

    Meta caps a template's whole text at 1,024 characters. Accepting twenty
    megabytes in order to be told that by the Graph API costs a database write,
    a queue job and a provider round trip to learn something this could have
    said immediately.
    """
    oversized = [{"type": "body", "parameters": [{"type": "text", "text": "x" * 20_000}]}]

    with pytest.raises(ValidationError) as error:
        model(**extra, **{field: oversized})

    assert error.value.errors()[0]["loc"] == (field,)


def test_a_real_template_payload_is_still_accepted() -> None:
    request = SendTemplateRequest(
        name="order_update",
        language="ar",
        components=[
            {"type": "header", "parameters": [{"type": "text", "text": "طلبك"}]},
            {"type": "body", "parameters": [{"type": "text", "text": "x" * 1024}]},
            {"type": "button", "sub_type": "url", "index": "0", "parameters": []},
        ],
    )

    assert request.components is not None
    assert len(request.components) == 3


# ------------------------------------------------------------ error privacy


@pytest.mark.parametrize(
    ("build", "field"),
    [
        pytest.param(
            lambda payload: LeadCreateRequest(**_lead(custom_fields=payload)),
            "custom_fields",
            id="lead-custom-fields",
        ),
        pytest.param(
            lambda payload: ToolGrantRequest(name="search_knowledge", config=payload),
            "config",
            id="tool-config",
        ),
        pytest.param(
            lambda payload: SendTemplateRequest(name="n", language="ar", components=[payload]),
            "components",
            id="template-components",
        ),
    ],
)
def test_a_refusal_never_repeats_what_it_refused(build: Any, field: str) -> None:
    """The rejected value is what made the request too large to keep.

    Echoing it into an error message would put it in every place a 422 body
    travels - proxy logs, APM payloads, error trackers - which is a worse
    outcome than the row it was preventing. `_safe_validation_errors` already
    strips `input` and `ctx`; this pins the half that is this module's own,
    which is that `msg` describes the constraint and never the content.
    """
    payload = {f"{CANARY}-{index}": CANARY * 100 for index in range(60)}

    with pytest.raises(ValidationError) as error:
        build(payload)

    messages = " ".join(entry["msg"] for entry in error.value.errors())
    assert CANARY not in messages
    assert field in messages
