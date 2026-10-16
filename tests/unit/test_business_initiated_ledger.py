"""The ledger's arithmetic: the campaign share and the service window's margin (LIM-03, LIM-04)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.business_initiated_ledger import (
    SERVICE_WINDOW_WITH_MARGIN,
    campaign_budget,
    window_open,
)


@pytest.mark.parametrize(
    ("limit", "budget"),
    [(50, 40), (250, 200), (2_000, 1_600), (10_000, 8_000), (100_000, 80_000), (None, None)],
)
def test_campaigns_get_eighty_percent_rounded_down(limit: int | None, budget: int | None) -> None:
    assert campaign_budget(limit, 0.8) == budget


def test_a_share_that_does_not_divide_rounds_down() -> None:
    assert campaign_budget(250, 0.81) == 202


def test_the_window_closes_an_hour_early() -> None:
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert timedelta(hours=23) == SERVICE_WINDOW_WITH_MARGIN
    assert window_open(now - timedelta(hours=22, minutes=59), at=now)
    assert not window_open(now - timedelta(hours=23), at=now)
    assert not window_open(now - timedelta(hours=23, minutes=30), at=now)
    assert not window_open(None, at=now)
