"""The rules a usage figure is only trustworthy under.

Three of them carry the phase: every meter knows what it counts, a caller cannot
choose the unit, and nothing that would corrupt a total ever becomes a row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ValidationError
from app.db.models.usage import (
    EVENT_UNITS,
    UsageEvent,
    UsageEventType,
    UsageUnit,
    unit_for,
)
from app.repositories.usage_repository import UsageTotal
from app.services.usage_service import (
    DEFAULT_WINDOW,
    MAX_WINDOW,
    UsageWindow,
    resolve_window,
    summarise,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def test_every_meter_declares_what_it_counts():
    """A member added without a unit has no answer to "how much"."""
    assert set(EVENT_UNITS) == set(UsageEventType)


def test_the_unit_comes_from_the_meter():
    assert unit_for(UsageEventType.AI_INPUT_TOKEN) is UsageUnit.TOKEN
    assert unit_for(UsageEventType.STORAGE_USED) is UsageUnit.BYTE
    assert unit_for(UsageEventType.VOICE_TRANSCRIPTION) is UsageUnit.SECOND
    assert unit_for(UsageEventType.WHATSAPP_MESSAGE_SENT) is UsageUnit.COUNT


def test_the_table_carries_no_updated_at():
    """Append-only. A column nothing can ever set is a claim the table cannot
    keep, and an update would make a past month's figure irreproducible."""
    assert "updated_at" not in UsageEvent.__table__.columns
    assert "occurred_at" in UsageEvent.__table__.columns


def test_the_aggregate_queries_have_indexes_to_run_on():
    names = {index.name for index in UsageEvent.__table__.indexes}
    assert "ix_usage_events_tenant_id" in names
    assert "ix_usage_events_tenant_id_occurred_at" in names
    assert "ix_usage_events_tenant_id_event_type_occurred_at" in names
    # The platform total spans every workspace, so no tenant-leading index
    # serves it.
    assert "ix_usage_events_occurred_at" in names


def test_an_unbounded_request_looks_back_thirty_days():
    window = resolve_window(now=NOW)
    assert window.until == NOW
    assert window.since == NOW - DEFAULT_WINDOW


def test_a_naive_time_is_read_as_utc():
    """The API declares its times in UTC, so this is unambiguous rather than
    sloppy - and refusing it gives a caller nothing to act on."""
    window = resolve_window(since=datetime(2026, 8, 1, 0, 0), now=NOW)
    assert window.since == datetime(2026, 8, 1, tzinfo=UTC)


def test_a_backwards_window_is_refused():
    with pytest.raises(ValidationError):
        resolve_window(since=NOW, until=NOW - timedelta(days=1))


def test_an_empty_window_is_refused():
    """`since == until` is a half-open range containing nothing, which is never
    what a caller meant."""
    with pytest.raises(ValidationError):
        resolve_window(since=NOW, until=NOW)


def test_a_window_wider_than_a_year_is_refused():
    with pytest.raises(ValidationError):
        resolve_window(since=NOW - MAX_WINDOW - timedelta(days=1), until=NOW)


def _total(event_type: UsageEventType, quantity: int) -> UsageTotal:
    return UsageTotal(
        event_type=event_type,
        unit=unit_for(event_type),
        quantity=quantity,
        events=1,
    )


def test_totals_fold_into_the_named_counters():
    window = UsageWindow(since=NOW - DEFAULT_WINDOW, until=NOW)
    summary = summarise(
        [
            _total(UsageEventType.WHATSAPP_MESSAGE_RECEIVED, 12),
            _total(UsageEventType.AI_INPUT_TOKEN, 900),
            _total(UsageEventType.AI_OUTPUT_TOKEN, 100),
        ],
        window=window,
    )
    assert summary.messages_received == 12
    assert summary.input_tokens == 900
    assert summary.output_tokens == 100
    assert summary.total_tokens == 1000
    # A meter nothing was recorded for reads as zero, not as absent.
    assert summary.campaign_messages == 0


def test_the_unabridged_totals_survive_the_fold():
    """A meter added later is visible before anything is renamed to carry it."""
    window = UsageWindow(since=NOW - DEFAULT_WINDOW, until=NOW)
    totals = [_total(UsageEventType.RAG_QUERY, 4)]
    assert summarise(totals, window=window).totals == tuple(totals)
