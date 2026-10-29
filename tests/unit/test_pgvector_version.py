"""The pgvector version knowledge search needs (RV-03, FX-1.3)."""

from __future__ import annotations

import pytest

from app.core.pgvector_version import MINIMUM_PGVECTOR_LABEL, parse_version, version_problem


@pytest.mark.parametrize("version", ["0.6.0", "0.7.4", "0.7.99", "", "unknown"])
def test_an_older_pgvector_is_refused_with_the_minimum_named(version: str) -> None:
    problem = version_problem(version)
    assert problem is not None
    assert MINIMUM_PGVECTOR_LABEL in problem


@pytest.mark.parametrize("version", ["0.8.0", "0.8.6", "0.9.0", "0.10.0", "0.10.1", "1.0.0"])
def test_a_supported_pgvector_passes(version: str) -> None:
    # 0.10.0 sorts before 0.8.0 as text: the comparison must be numeric (M-R41).
    assert version_problem(version) is None


def test_no_pgvector_at_all_is_left_to_the_extension_check() -> None:
    assert version_problem(None) is None


def test_versions_are_parsed_as_numbers() -> None:
    assert parse_version("0.10.1") == (0, 10, 1)
    assert parse_version("0.10.1") > parse_version("0.8.6")
