"""The pure rules of a person's own values (PER-01, PER-03, PER-05)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.services.personalisation import (
    META_BODY_MAX_CHARS,
    Mapping,
    ParameterSource,
    PersonFacts,
    Rendered,
    fill,
    normalise,
    resolve,
)

NAME = Mapping(ParameterSource.CONTACT_DISPLAY_NAME, fallback="friend")
SALE = Mapping(ParameterSource.CONSTANT, value="spring")


def _facts(**fields: object) -> PersonFacts:
    return PersonFacts(contact_id=uuid.uuid4(), **fields)  # type: ignore[arg-type]


def test_normalise_makes_one_trimmed_line_without_control_characters() -> None:
    assert normalise("  a\nb\r\nc\t\td          e \x00\x07 ") == "a b c d e"
    assert normalise(None) == ""
    assert normalise("\n\t ") == ""
    assert normalise("محمد") == "محمد"


def test_a_missing_empty_or_long_value_is_the_fallback() -> None:
    for name in (None, "", " \n ", "x" * 61):
        rendered = resolve(
            body=(NAME, SALE),
            header=(),
            facts=_facts(display_name=name),
            max_chars=60,
            body_text="Hi {{1}} {{2}}",
            body_names=("1", "2"),
        )
        assert rendered.body == ("friend", "spring")
        assert rendered.body_fallbacks == (0,)


def test_a_person_s_own_value_is_used_and_constants_never_fall_back() -> None:
    rendered = resolve(
        body=(NAME, SALE),
        header=(),
        facts=_facts(display_name="Ahmed"),
        max_chars=60,
        body_text="Hi {{1}} {{2}}",
        body_names=("1", "2"),
    )
    assert rendered.body == ("Ahmed", "spring") and rendered.body_fallbacks == ()


def test_a_body_past_meta_s_limit_takes_every_fallback() -> None:
    text = "{{1}}" + "." * (META_BODY_MAX_CHARS - 10)
    rendered = resolve(
        body=(NAME,),
        header=(),
        facts=_facts(display_name="y" * 20),
        max_chars=60,
        body_text=text,
        body_names=("1",),
    )
    assert rendered.body == ("friend",) and rendered.body_too_long


def test_lead_fields_read_as_text() -> None:
    facts = _facts(
        lead_status="qualified",
        lead_budget_amount=Decimal("500000.00"),
        lead_budget_currency="EGP",
        lead_custom_fields={"area": 150, "nested": {"no": 1}},
    )
    mappings = (
        Mapping(ParameterSource.LEAD_STATUS, fallback="-"),
        Mapping(ParameterSource.LEAD_BUDGET, fallback="-"),
        Mapping(ParameterSource.LEAD_CUSTOM_FIELD, key="area", fallback="-"),
        Mapping(ParameterSource.LEAD_CUSTOM_FIELD, key="nested", fallback="-"),
    )
    rendered = resolve(
        body=mappings, header=(), facts=facts, max_chars=60, body_text=None, body_names=()
    )
    assert rendered.body == ("Qualified", "500,000 EGP", "150", "-")


def test_named_and_positional_placeholders_fill_in_order() -> None:
    assert fill("Hi {{ name }}, {{offer}}", ("name", "offer"), ("A", "B")) == "Hi A, B"
    assert fill("{{1}} and {{2}}", ("1", "2"), ("x", "\\1")) == "x and \\1"


def test_rendered_values_round_trip_with_their_contact() -> None:
    rendered = Rendered(
        contact_id=uuid.uuid4(),
        body=("a",),
        header=("b",),
        body_fallbacks=(0,),
        header_fallbacks=(),
    )
    assert Rendered.from_dict(rendered.as_dict()) == rendered


def test_the_phone_number_is_not_a_source() -> None:
    assert "contact.wa_id" not in {source.value for source in ParameterSource}
