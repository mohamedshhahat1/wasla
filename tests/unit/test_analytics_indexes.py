"""Every analytics window has an index that covers it.

Phase 12 added a family of queries shaped "this workspace, this window" and
indexed none of them, so PostgreSQL found the workspace's rows and discarded
most of them by filter — waste proportional to how long a workspace had existed,
which made the dashboard slowest for the longest-paying customers.

This is the guard against adding the next one the same way. It reads metadata
rather than a database, so it runs in the unit suite; the measurement that
motivated it is in migration 0019.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table

from app.db.models import Base

# Tables the tenant analytics service filters by `(tenant_id, created_at)`.
# Adding a metric over a new table means adding it here, which is the point:
# the failure is a missing index, and the reminder should arrive with the query.
WINDOWED_TABLES = (
    "conversations",
    "messages",
    "leads",
    "message_sentiments",
    "campaign_recipients",
    "analytics_events",
)


def _covers_window(table: Table, timestamp: str) -> bool:
    """Whether an index leads with `(tenant_id, <timestamp>)`.

    Both columns and in that order. A tenant-only index finds the workspace and
    then filters; the timestamp has to be the second column for the range to be
    read from the index rather than from the heap.
    """
    wanted = [table.columns["tenant_id"], table.columns[timestamp]]
    return any(list(index.columns)[:2] == wanted for index in table.indexes)


@pytest.mark.parametrize("name", WINDOWED_TABLES)
def test_a_windowed_table_indexes_its_window(name: str) -> None:
    table = Base.metadata.tables[name]
    # `usage_events` and `analytics_events` time their rows with `occurred_at`;
    # everything else uses the row's creation.
    timestamp = "occurred_at" if "occurred_at" in table.columns else "created_at"
    assert _covers_window(
        table, timestamp
    ), f"{name} is read by tenant and {timestamp} and has no index leading with both"


def test_usage_events_index_their_own_window() -> None:
    """Phase 12 got this one right, and it is the reason the others were noticed:
    the usage figures were fast while the analytics figures were not."""
    table = Base.metadata.tables["usage_events"]
    assert _covers_window(table, "occurred_at")


def test_the_platform_window_is_indexed_without_a_tenant() -> None:
    """A platform total spans every workspace, so no tenant-leading index can
    serve it - it needs one on the timestamp alone."""
    table = Base.metadata.tables["usage_events"]
    assert any(list(index.columns) == [table.columns["occurred_at"]] for index in table.indexes)
