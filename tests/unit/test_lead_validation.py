"""Validation of what reaches a lead, from people and from models.

The budget parser gets the most attention here because it is the field where
being wrong is expensive and silent: a lead that says 500 instead of 500,000
sorts to the bottom of a rep's queue and is never looked at again.

The private helpers are exercised directly. They are the seam where a model's
guess is accepted or dropped, and reaching them through the service would need
a database for rules that involve none.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import ValidationError
from app.schemas.lead import LeadUpdateRequest
from app.services.lead_service import (
    MAX_TAGS,
    UNSET,
    ExtractedLead,
    _validated,
    _validated_budget,
    _validated_tags,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (500000, Decimal("500000.00")),
        ("500000", Decimal("500000.00")),
        # Thousands separators are unambiguous, so they are handled.
        ("500,000", Decimal("500000.00")),
        (" 1200.50 ", Decimal("1200.50")),
        (0, Decimal("0.00")),
    ],
)
def test_a_budget_is_parsed_when_it_is_unambiguous(raw, expected):
    assert _validated_budget(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Deliberately unsupported. "500k" means 500,000 to most people and 500
        # to a parser that gives up, and guessing wrong reprioritises a real
        # pipeline silently.
        "500k",
        "half a million",
        "",
        "abc",
        "NaN",
        True,
        -1,
    ],
)
def test_an_unparseable_or_impossible_budget_is_refused(raw):
    with pytest.raises(ValidationError):
        _validated_budget(raw)


def test_a_budget_beyond_the_column_is_refused():
    """Better a clear error than a database overflow mid-request."""
    with pytest.raises(ValidationError):
        _validated_budget("9" * 15)


def test_a_model_guess_is_dropped_rather_than_failing_the_whole_capture():
    """Lenient mode: one bad field must not lose the others."""
    cleaned = _validated(
        {"name": "Ahmed", "email": "not-an-email", "budget_amount": "500k"},
        lenient=True,
    )

    assert cleaned == {"name": "Ahmed"}


def test_a_persons_bad_input_is_reported_rather_than_dropped():
    """Strict mode: someone typing into a form deserves to be told."""
    with pytest.raises(ValidationError):
        _validated({"email": "not-an-email"})


@pytest.mark.parametrize("raw", ["ahmed@example.com", "AHMED@Example.COM"])
def test_an_email_is_accepted_and_normalised(raw):
    assert _validated({"email": raw})["email"] == "ahmed@example.com"


@pytest.mark.parametrize("raw", ["ahmed@", "@example.com", "ahmed example.com", "a@b"])
def test_text_that_is_not_an_email_is_refused(raw):
    with pytest.raises(ValidationError):
        _validated({"email": raw})


@pytest.mark.parametrize("raw", ["+20 100 123 4567", "01001234567", "(202) 555-0143"])
def test_a_phone_number_keeps_the_punctuation_people_type(raw):
    assert _validated({"phone": raw})["phone"] == raw.strip()


@pytest.mark.parametrize("raw", ["call me", "12", "+" + "9" * 40])
def test_text_that_is_not_a_phone_number_is_refused(raw):
    with pytest.raises(ValidationError):
        _validated({"phone": raw})


def test_a_currency_must_be_a_three_letter_code():
    assert _validated({"budget_currency": "egp"})["budget_currency"] == "EGP"
    with pytest.raises(ValidationError):
        _validated({"budget_currency": "Egyptian Pounds"})


def test_a_blank_field_is_not_a_value():
    """An empty string would otherwise overwrite a real name with nothing."""
    assert _validated({"name": "   "}, lenient=True) == {}


def test_null_clears_a_field_only_when_clearing_is_allowed():
    assert _validated({"email": None}, allow_null=True) == {"email": None}
    # Creation passes no nulls: an absent field is absent, not cleared.
    assert _validated({"email": None}) == {}


def test_tags_are_normalised_and_deduplicated():
    assert _validated_tags([" Hot ", "HOT", "cairo", ""]) == ["hot", "cairo"]


def test_too_many_tags_are_refused():
    with pytest.raises(ValidationError):
        _validated_tags([f"tag-{index}" for index in range(MAX_TAGS + 1)])


def test_an_overlong_tag_is_refused():
    with pytest.raises(ValidationError):
        _validated_tags(["x" * 51])


def test_extraction_reports_only_the_fields_it_actually_found():
    extracted = ExtractedLead(name="Ahmed", email=None, interest="")

    assert extracted.as_fields() == {"name": "Ahmed"}


def test_an_empty_extraction_reports_nothing():
    assert ExtractedLead().as_fields() == {}


def test_an_omitted_field_is_left_alone_and_an_explicit_null_clears_it():
    """The distinction the sentinel exists for.

    Sending `{"email": null}` must clear the address; sending `{}` must not.
    Pydantic cannot tell those apart from the value, so omission is read from
    `model_fields_set`.
    """
    cleared = LeadUpdateRequest.model_validate({"email": None}).to_update()
    untouched = LeadUpdateRequest.model_validate({}).to_update()

    assert cleared.email is None
    assert untouched.email is UNSET


def test_supplying_a_value_carries_it_through():
    update = LeadUpdateRequest.model_validate({"name": "Ahmed"}).to_update()

    assert update.name == "Ahmed"
    assert update.phone is UNSET
