"""The images install their locked dependencies before they copy the code (DK, ADR-201).

Docker reuses a layer only while every input of it, and of the layers before
it, is unchanged. When the application source is copied before the
third-party install, any edit under ``app/`` invalidates that install, so a
code-only rebuild downloads and stores the whole dependency set again. These
tests keep the order that lets a code change reuse the dependency layer, and
keep the hash pinning the order must never weaken.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every Dockerfile that installs Python distributions for the application.
PYTHON_IMAGES = ("Dockerfile", "tests/e2e_stack/Dockerfile")

# Source trees whose edits must not reach the dependency layer.
SOURCE_PATHS = ("app", "alembic", "scripts", "tests", ".")

PACKAGE_INSTALL = re.compile(
    r"pip install (?:[^&]*\s)?(?:-e\s+)?[\"']?\.(?:\[[a-z,]+\])?[\"']?(?:\s|$)"
)

LOCKED_INSTALL = "pip install --require-hashes -r requirements.lock -r requirements-build.lock"

# The runtime stage of the production image, as it stood before the reorder.
# DK moves lines inside the builder stage only; a change here is a change to
# what ships and needs its own decision, not this one.
RUNTIME_STAGE_SHA256 = "7af44b5ddd02e72e177640597fbdfec237ec53eb146b3fb367a7d1ebc521c9e1"


def _instructions(text: str) -> list[tuple[str, str]]:
    """Return ``(stage, instruction)`` pairs with continuation lines joined."""
    joined = re.sub(r"\\\n", " ", text)
    stage = ""
    result: list[tuple[str, str]] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if match:
            stage = match.group(1) or line
        result.append((stage, line))
    return result


def _copies_source(instruction: str) -> bool:
    if not instruction.startswith(("COPY ", "ADD ")) or "--from=" in instruction:
        return False
    sources = instruction.split()[1:-1]
    return any(source.rstrip("/") in SOURCE_PATHS for source in sources)


def _installs_locked_dependencies(instruction: str) -> bool:
    return instruction.startswith("RUN ") and LOCKED_INSTALL in instruction


@pytest.mark.parametrize("name", PYTHON_IMAGES)
def test_no_source_copy_precedes_the_locked_install(name: str) -> None:
    instructions = _instructions((ROOT / name).read_text(encoding="utf-8"))
    installs = [
        at for at, (_, line) in enumerate(instructions) if _installs_locked_dependencies(line)
    ]
    assert installs, f"{name} no longer installs the locks by hash"
    for index in installs:
        stage = instructions[index][0]
        earlier = [
            line
            for at, (other_stage, line) in enumerate(instructions[:index])
            if other_stage == stage and _copies_source(line)
        ]
        assert not earlier, f"{name}: source copied before the locked install: {earlier}"


@pytest.mark.parametrize("name", PYTHON_IMAGES)
def test_locked_install_keeps_its_hashes_and_lock_files(name: str) -> None:
    instructions = _instructions((ROOT / name).read_text(encoding="utf-8"))
    locked = [line for _, line in instructions if _installs_locked_dependencies(line)]
    assert len(locked) == 1, name
    # The locked install installs nothing else in the same step: a package
    # install chained here would put the code back into the dependency layer.
    assert " -e " not in locked[0] and " . " not in f"{locked[0]} ", name


@pytest.mark.parametrize("name", PYTHON_IMAGES)
def test_the_package_itself_is_installed_without_its_dependencies(name: str) -> None:
    instructions = _instructions((ROOT / name).read_text(encoding="utf-8"))
    package_installs = [
        line for _, line in instructions if line.startswith("RUN ") and PACKAGE_INSTALL.search(line)
    ]
    assert package_installs, f"{name} does not install the package"
    for line in package_installs:
        assert "--no-deps" in line, f"{name}: {line}"


def test_production_runtime_stage_is_unchanged() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = text[text.index("FROM base AS runtime") :]
    assert hashlib.sha256(runtime.encode("utf-8")).hexdigest() == RUNTIME_STAGE_SHA256
