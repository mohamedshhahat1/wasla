"""The e2e gate-state helper stays test-only (FE-3, EC-04).

``tests.e2e_stack.gate_states`` writes subscription and usage rows straight
through the models, which is fine for a stack that is never deployed and wrong
anywhere else - so no module of the application may import anything from
``tests``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.e2e_stack import gate_states

APP = Path(__file__).resolve().parents[2] / "app"


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_no_application_module_imports_the_tests() -> None:
    offenders = [
        f"{path.relative_to(APP.parent)}: {name}"
        for path in sorted(APP.rglob("*.py"))
        for name in _imports_of(path)
        if name == "tests" or name.startswith("tests.")
    ]
    assert offenders == []


def test_the_scan_sees_a_test_import(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("from tests.e2e_stack import gate_states\nimport tests.fakes\n")
    assert _imports_of(probe) == ["tests.e2e_stack", "tests.fakes"]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["trialing"],
        ["trialing", "a-slug"],
        ["trialing", "a-slug", "soon"],
        ["past-due"],
        ["past-due", "a-slug", "3"],
        ["unknown", "a-slug"],
    ],
)
def test_an_incomplete_command_is_refused_with_the_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        gate_states.parse(argv)
    assert "usage" in str(refused.value)


def test_the_commands_parse() -> None:
    assert gate_states.parse(["trialing", "acme", "3"]) == ("trialing", "acme", 3)
    assert gate_states.parse(["past-due", "acme"]) == ("past-due", "acme", None)
    assert gate_states.parse(["ai-allowance-used", "acme"]) == ("ai-allowance-used", "acme", None)
