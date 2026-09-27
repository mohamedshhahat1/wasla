"""No list is ordered by a timestamp alone (DB-013).

Rows written in one transaction share `now()`: a settlement writes two audit
entries at once, an invitation batch writes several memberships at once. A
list ordered by that timestamp alone - and paged or limited - may come back in
a different order each time, and a page boundary can then repeat one row and
skip another. Every such ordering now ends with the row's `id`.

This reads the source rather than running queries, so a new `order_by` on a
timestamp column cannot ship without a tie-breaker.
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

# `.order_by(Model.some_at)` or `.order_by(Model.some_at.desc())`, closed at once.
TIMESTAMP_ONLY = re.compile(
    r"order_by\(\s*[A-Za-z_]+\.[a-z_]+(?:_at|_start|_end)(?:\.desc\(\)|\.asc\(\))?\s*\)"
)


def test_every_timestamp_ordering_has_a_tie_breaker() -> None:
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}"
        for path in sorted(APP.rglob("*.py"))
        for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if TIMESTAMP_ONLY.search(text)
    ]
    assert offenders == []
