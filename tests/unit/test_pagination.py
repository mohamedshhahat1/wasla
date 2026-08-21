"""The cursor codec.

A cursor arrives in a query string, which means it arrives mangled by proxies,
truncated by clients and occasionally fuzzed. Every one of those has to be a
422, never a 500, so most of this file is about what happens to bad input.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ValidationError
from app.core.pagination import MAX_CURSOR_LENGTH, Cursor, Page, paginate

MOMENT = datetime(2026, 8, 21, 12, 30, 45, 123456, tzinfo=UTC)
ROW_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_a_cursor_survives_a_round_trip():
    cursor = Cursor(sort_value=MOMENT, id=ROW_ID)

    assert Cursor.decode(cursor.encode()) == cursor


def test_microseconds_survive():
    """Truncating here would make two rows in the same second inseparable."""
    cursor = Cursor(sort_value=MOMENT, id=ROW_ID)

    assert Cursor.decode(cursor.encode()).sort_value == MOMENT


def test_a_null_sort_value_survives():
    """Rows with no sort value order last, and paging through them still works."""
    cursor = Cursor(sort_value=None, id=ROW_ID)

    decoded = Cursor.decode(cursor.encode())
    assert decoded.sort_value is None
    assert decoded.id == ROW_ID


def test_a_cursor_is_url_safe_and_unpadded():
    encoded = Cursor(sort_value=MOMENT, id=ROW_ID).encode()

    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("!!!not base64!!!", id="not-base64"),
        pytest.param("x" * (MAX_CURSOR_LENGTH + 1), id="too-long"),
    ],
)
def test_malformed_cursors_are_rejected(raw):
    with pytest.raises(ValidationError):
        Cursor.decode(raw)


def _encoded(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode()).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("no-separator-at-all", id="no-separator"),
        pytest.param(f"{MOMENT.isoformat()}|not-a-uuid", id="bad-uuid"),
        pytest.param(f"not-a-date|{ROW_ID}", id="bad-timestamp"),
        pytest.param("|", id="both-empty"),
    ],
)
def test_wellformed_base64_carrying_nonsense_is_rejected(payload):
    """Decoding must not be the same as trusting."""
    with pytest.raises(ValidationError):
        Cursor.decode(_encoded(payload))


def test_a_naive_timestamp_is_pinned_to_utc():
    """Every timestamp column is aware, so a naive value would compare wrongly."""
    decoded = Cursor.decode(_encoded(f"2026-08-21T12:30:45|{ROW_ID}"))

    assert decoded.sort_value is not None
    assert decoded.sort_value.tzinfo is not None
    assert decoded.sort_value == datetime(2026, 8, 21, 12, 30, 45, tzinfo=UTC)


def test_a_rejected_cursor_answers_422():
    with pytest.raises(ValidationError) as raised:
        Cursor.decode("!!!")

    assert raised.value.status_code == 422


class Row:
    def __init__(self, moment, row_id):
        self.moment = moment
        self.id = row_id


def _key(row: Row) -> Cursor:
    return Cursor(sort_value=row.moment, id=row.id)


def _rows(count: int) -> list[Row]:
    return [Row(MOMENT - timedelta(minutes=index), uuid.uuid4()) for index in range(count)]


def test_a_full_page_offers_a_cursor():
    rows = _rows(3)

    page = paginate(rows, limit=3, key=_key)

    assert page.next_cursor is not None
    assert Cursor.decode(page.next_cursor) == _key(rows[-1])


def test_a_short_page_ends_the_collection():
    """Issuing a cursor here would invite a request guaranteed to return nothing."""
    page = paginate(_rows(2), limit=3, key=_key)

    assert page.next_cursor is None


def test_an_empty_page_ends_the_collection():
    page: Page[Row] = paginate([], limit=3, key=_key)

    assert page.items == []
    assert page.next_cursor is None


def test_the_cursor_names_the_last_row_not_the_first():
    """Naming the first would replay the page just handed out."""
    rows = _rows(3)

    page = paginate(rows, limit=3, key=_key)

    assert Cursor.decode(page.next_cursor or "").id == rows[-1].id
