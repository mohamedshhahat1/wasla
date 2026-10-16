"""The zone database a workspace's time is computed in (TZ-01, TZ-02)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core import timezones


def test_cairo_observes_daylight_saving() -> None:
    assert timezones.describe("Africa/Cairo", datetime(2026, 1, 15, tzinfo=UTC)).utc_offset == (
        "+02:00"
    )
    assert timezones.describe("Africa/Cairo", datetime(2026, 7, 15, tzinfo=UTC)).utc_offset == (
        "+03:00"
    )


def test_offsets_are_formatted_with_sign_hours_and_minutes() -> None:
    at = datetime(2026, 1, 15, tzinfo=UTC)
    assert timezones.describe("Asia/Kolkata", at).utc_offset == "+05:30"
    assert timezones.describe("America/St_Johns", at).utc_offset == "-03:30"
    assert timezones.describe("Europe/London", at).utc_offset == "+00:00"


def test_every_suggested_zone_is_canonical() -> None:
    assert set(timezones.SUGGESTED_TIMEZONES) <= timezones.CANONICAL_TIMEZONES


@pytest.mark.parametrize("alias", ["US/Eastern", "Egypt", "Etc/GMT+3", "Asia/Calcutta", "UTC"])
def test_an_alias_is_not_a_zone_a_workspace_may_choose(alias: str) -> None:
    with pytest.raises(timezones.UnknownTimezoneError) as refused:
        timezones.validated(alias)
    assert refused.value.error_code == "timezone_unknown"
    assert refused.value.status_code == 422


def test_zones_come_from_the_pinned_package_not_the_system() -> None:
    zone = timezones.zone("Africa/Cairo")
    assert getattr(zone, "key", None) == "Africa/Cairo"
    assert timezones.zone("Africa/Cairo") is zone


def test_the_etag_changes_with_the_query_and_not_otherwise() -> None:
    at = datetime(2026, 10, 16, tzinfo=UTC)
    assert timezones.listing_etag(at=at) == timezones.listing_etag(at=at)
    assert timezones.listing_etag(at=at) != timezones.listing_etag(at=at, query="cairo")
    assert timezones.listing_etag(at=at, query="Cairo") == timezones.listing_etag(
        at=at, query="cairo"
    )


def test_the_etag_changes_when_an_offset_does() -> None:
    winter = datetime(2026, 1, 15, tzinfo=UTC)
    summer = datetime(2026, 7, 15, tzinfo=UTC)
    assert timezones.listing_etag(at=winter) != timezones.listing_etag(at=summer)
