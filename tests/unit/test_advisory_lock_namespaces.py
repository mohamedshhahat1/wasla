"""Advisory-lock namespaces are declared once and never shared (DB-016).

Password resets and platform-owner changes both used namespace `0x57415302`,
and both declarations said they were distinct. A user whose id's low bits
matched the owner key serialised resets behind owner changes for no reason.
"""

from __future__ import annotations

import pathlib
import re

from app.db import advisory_locks
from app.platform import hierarchy
from app.services import password_reset_service, workspace_service

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
REGISTRY = APP / "db" / "advisory_locks.py"
# The application's namespaces all start 0x5741_53.. ("WAS"), with or without
# the digit separator.
NAMESPACE_LITERAL = re.compile(r"0x5741_?53[0-9A-Fa-f]{2}")


def test_every_namespace_is_distinct() -> None:
    values = list(advisory_locks.NAMESPACES.values())
    assert len(values) == len(set(values)), advisory_locks.NAMESPACES


def test_the_services_use_the_registry() -> None:
    assert hierarchy._PLATFORM_LOCK_NAMESPACE == advisory_locks.PLATFORM_OWNERS
    assert (
        password_reset_service._RESET_REQUEST_LOCK_NAMESPACE
        == advisory_locks.PASSWORD_RESET_REQUEST
    )
    assert workspace_service._WORKSPACE_CREATION_LOCK_NAMESPACE == advisory_locks.WORKSPACE_CREATION


def test_no_namespace_is_declared_outside_the_registry() -> None:
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}"
        for path in sorted(APP.rglob("*.py"))
        if path != REGISTRY
        for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if NAMESPACE_LITERAL.search(text)
    ]
    assert offenders == []
