"""The inbound counters' labels are closed, and closed to the right sets (OMNI-021).

`app.core.telemetry` imports nothing from `app.channels`, so it restates the
refusal reasons and outcomes it accepts. These tests hold the restatement equal
to the vocabulary it mirrors, so a new refusal reason is counted under its own
name rather than silently as `other` - and so no caller, and nothing a provider
sends, can widen a label domain.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels.inbound import RefusalReason
from app.core import telemetry
from app.core.telemetry import (
    INBOUND_OUTCOMES,
    INBOUND_REFUSAL_REASONS,
    REDIS_COUNTERS,
    record_inbound_outcomes,
    record_inbound_refusals,
)
from app.db.models.channel import Channel


def test_the_refusal_domain_is_the_adapters_vocabulary() -> None:
    assert frozenset(reason.value for reason in RefusalReason) == INBOUND_REFUSAL_REASONS


def test_both_inbound_counters_are_declared_with_closed_labels() -> None:
    assert REDIS_COUNTERS["wasla_inbound_entries_refused_total"][1] == ("channel", "reason")
    assert REDIS_COUNTERS["wasla_inbound_events_total"][1] == ("channel", "outcome")


@pytest.fixture
def increments(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str], int]]:
    seen: list[tuple[str, dict[str, str], int]] = []

    async def record(metric: str, labels: dict[str, str], amount: int) -> None:
        seen.append((metric, dict(labels), amount))

    monkeypatch.setattr(telemetry, "_increment_by", record)
    return seen


async def test_an_unknown_reason_or_channel_is_counted_as_other(
    increments: list[tuple[str, dict[str, str], Any]],
) -> None:
    await record_inbound_refusals(
        "telegram", {"missing_sender": 2, "a reason from a payload": 1, "malformed": 0}
    )

    assert increments == [
        (
            "wasla_inbound_entries_refused_total",
            {"channel": "other", "reason": "missing_sender"},
            2,
        ),
        ("wasla_inbound_entries_refused_total", {"channel": "other", "reason": "other"}, 1),
    ]


async def test_outcomes_keep_their_channel_and_close_their_outcome(
    increments: list[tuple[str, dict[str, str], Any]],
) -> None:
    await record_inbound_outcomes(Channel.WHATSAPP.value, {"echo": 1, "invented": 3, "stored": 0})

    assert increments == [
        ("wasla_inbound_events_total", {"channel": "whatsapp", "outcome": "echo"}, 1),
        ("wasla_inbound_events_total", {"channel": "whatsapp", "outcome": "other"}, 3),
    ]
    assert "echo" in INBOUND_OUTCOMES and "collision" in INBOUND_OUTCOMES
