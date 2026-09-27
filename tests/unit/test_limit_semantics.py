"""What a stored plan limit means, pinned to the documentation (DB-026)."""

from __future__ import annotations

import pytest

from app.db.models.billing import validated_limit


@pytest.mark.parametrize(
    ("raw", "limit"),
    [
        (None, None),  # missing or JSON null: unlimited
        (0, 0),  # zero is zero - a plan may exclude a feature (ADR-113)
        (5, 5),
        (-1, None),  # malformed: unlimited, never zero
        ("5", None),
        (5.5, None),
        (True, None),
        (False, None),
    ],
)
def test_a_stored_limit_reads_as_documented(raw: object, limit: int | None) -> None:
    assert validated_limit(raw) == limit
