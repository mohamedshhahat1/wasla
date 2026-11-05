"""Fail when an installed package is not the version a lock file pins (RGM-07).

CI installs `requirements.lock` first, the way the image does, so the tests run
on the packages that ship. This is the check that it worked: every package the
lock names must be installed at exactly the pinned version. A package pip
resolved to something else - pypdf drifted CI red three times that way - or one
that is missing is printed and the exit status is 1. Packages the lock does not
name (the dev tools) are not judged.

Usage: python -m scripts.check_installed_lock requirements.lock [more.lock ...]
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


def canonical(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(lock: Path) -> dict[str, str]:
    """`name -> version` for every `name==version` line of a pip-compile style lock."""
    found: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            found[canonical(match.group(1))] = match.group(2)
    return found


def installed() -> dict[str, str]:
    return {canonical(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}


def drift(expected: dict[str, str], present: dict[str, str]) -> list[str]:
    """One line per pinned package that is missing or at another version."""
    return [
        f"{name}: lock {version}, installed {present.get(name, 'nothing')}"
        for name, version in sorted(expected.items())
        if present.get(name) != version
    ]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1])  # noqa: T201
        return 2
    expected: dict[str, str] = {}
    for path in argv:
        expected.update(pins(Path(path)))
    differences = drift(expected, installed())
    for line in differences:
        print(f"drift: {line}")  # noqa: T201
    print(  # noqa: T201
        f"{len(expected)} pinned packages checked, {len(differences)} differ from the lock"
    )
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
