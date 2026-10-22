"""Meta's messaging-limit values, and the webhook fields that carry them (LIM-01)."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.whatsapp.payload import (
    SUPPORTED_FIELDS,
    account_alert_events,
    capability_events,
)
from app.services.messaging_limit_service import LOWEST_LEVEL, interpret


@pytest.mark.parametrize(
    ("value", "limit"),
    [
        ("TIER_50", 50),
        ("TIER_250", 250),
        ("TIER_2K", 2_000),
        ("TIER_10K", 10_000),
        ("TIER_100K", 100_000),
        ("TIER_UNLIMITED", None),
        (250, 250),
        (2000, 2_000),
        (100000, 100_000),
        (-1, None),
        ("2000", 2_000),
    ],
)
def test_every_documented_value_is_its_number(value: str | int, limit: int | None) -> None:
    reading = interpret(value)
    assert reading.recognised
    assert reading.limit == limit


@pytest.mark.parametrize("value", ["UNTIERED", "TIER_NOT_SET", "TIER_1M", "", 7, 3000, True])
def test_anything_else_is_the_lowest_level_never_more(value: str | int) -> None:
    reading = interpret(value)
    assert not reading.recognised
    assert reading.limit == LOWEST_LEVEL == 250


def _delivery(
    field: str, value: dict[str, Any], *, waba: str = "1234", time: int = 1739321024
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba, "time": time, "changes": [{"field": field, "value": value}]}],
    }


def test_a_capability_update_carries_the_portfolio_limit() -> None:
    events = capability_events(
        _delivery(
            "business_capability_update",
            {"max_daily_conversations_per_business": "TIER_2K", "max_phone_numbers_per_waba": 25},
        )
    )
    assert [(event.waba_id, event.value) for event in events] == [("1234", "TIER_2K")]
    assert events[0].at is not None


def test_meta_s_own_integer_example_and_the_deprecated_field_are_read() -> None:
    integer = capability_events(
        _delivery("business_capability_update", {"max_daily_conversations_per_business": 2000})
    )
    deprecated = capability_events(
        _delivery("business_capability_update", {"max_daily_conversation_per_phone": -1})
    )
    assert integer[0].value == 2000
    assert deprecated[0].value == -1


def test_a_capability_update_without_a_limit_is_nothing() -> None:
    assert capability_events(_delivery("business_capability_update", {"other": 1})) == []
    assert capability_events({"object": "page", "entry": []}) == []


def test_account_alerts_fold_into_a_closed_set() -> None:
    known = account_alert_events(
        _delivery("account_alerts", {"alert_type": "INCREASED_CAPABILITIES_ELIGIBILITY_FAILED"})
    )
    other = account_alert_events(_delivery("account_alerts", {"alert_type": "SOMETHING_NEW"}))
    assert known[0].alert_type == "INCREASED_CAPABILITIES_ELIGIBILITY_FAILED"
    assert other[0].alert_type == "other"


def test_both_fields_are_handled_rather_than_refused() -> None:
    assert {"business_capability_update", "account_alerts"} <= SUPPORTED_FIELDS
