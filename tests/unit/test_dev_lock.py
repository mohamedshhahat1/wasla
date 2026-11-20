"""The dev tools are installed by hash, from one list (FEB-43, DH).

`requirements-dev.lock` is compiled from `requirements-dev.in`, which mirrors
pyproject.toml's `dev` extra. A pin changed in one and not the other would
install a tool nobody reviewed, so the two lists are compared here; and no
workflow or Dockerfile may install the dev tools any other way.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pins(lines: list[str]) -> set[str]:
    return {
        line.split("#", 1)[0].strip().lower().replace("_", "-")
        for line in lines
        if line.split("#", 1)[0].strip()
    }


def test_the_dev_lock_inputs_are_pyprojects_dev_extra() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = _pins(project["project"]["optional-dependencies"]["dev"])
    inputs = _pins((ROOT / "requirements-dev.in").read_text(encoding="utf-8").splitlines())
    assert inputs == declared
    assert all("==" in pin for pin in inputs), "every dev tool is pinned exactly"


def test_every_dev_lock_requirement_carries_a_hash() -> None:
    text = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=\S)", text)
    requirements = [entry for entry in entries if re.match(r"[A-Za-z0-9]", entry)]
    assert requirements, "the dev lock names nothing"
    for entry in requirements:
        assert "--hash=sha256:" in entry, entry.splitlines()[0]


def test_the_dev_lock_pins_each_tool_at_its_declared_version() -> None:
    text = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8").lower().replace("_", "-")
    locked = set(re.findall(r"^([a-z0-9.-]+==\S+)", text, re.M))
    inputs = _pins((ROOT / "requirements-dev.in").read_text(encoding="utf-8").splitlines())
    assert inputs <= locked


def test_nothing_installs_the_dev_tools_without_hashes() -> None:
    sources = [*sorted((ROOT / ".github" / "workflows").glob("*.yml"))]
    sources += [ROOT / "Dockerfile", ROOT / "tests" / "e2e_stack" / "Dockerfile"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""pip install[^\n]*\.\[dev\]""", text), path.name
        assert "optional-dependencies']['dev']" not in text, path.name
        for line in text.splitlines():
            if "-r requirements-dev.lock" in line:
                assert "--require-hashes" in line, (path.name, line)
