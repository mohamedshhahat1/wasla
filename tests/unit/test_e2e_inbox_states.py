"""The e2e inbox-state helper refuses an incomplete command before it connects (FE-4, EC4-04).

That no application module imports ``tests`` is held for the whole package by
``tests/unit/test_e2e_gate_states.py``.
"""

from __future__ import annotations

import uuid

import pytest

from tests.e2e_stack import inbox_states

ID = str(uuid.UUID(int=7))


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["window-closed"],
        ["window-closed", "not-an-id"],
        ["templates"],
        ["media", ID],
        ["media", ID, "available"],
        ["media", "not-an-id", "failed"],
        ["voice-note", ID, "extra"],
        ["unknown", ID],
    ],
)
def test_an_incomplete_command_is_refused_with_the_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        inbox_states.parse(argv)
    assert refused.value.code == inbox_states.USAGE


@pytest.mark.parametrize(
    ("argv", "parsed"),
    [
        (["window-closed", ID], ("window-closed", ID, None)),
        (["templates", "a-slug"], ("templates", "a-slug", None)),
        (["media", ID, "pending"], ("media", ID, "pending")),
        (["media", ID, "failed"], ("media", ID, "failed")),
        (["media", ID, "unavailable"], ("media", ID, "unavailable")),
        (["voice-note", ID], ("voice-note", ID, None)),
    ],
)
def test_a_complete_command_is_read_as_written(
    argv: list[str], parsed: tuple[str, str, str | None]
) -> None:
    assert inbox_states.parse(argv) == parsed
