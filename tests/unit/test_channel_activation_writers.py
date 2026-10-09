"""Every code path that makes a channel connection active goes through the guard (ENT-08).

A connection takes a channel slot the moment it is written active - a WhatsApp
number connected or enabled, a connection of any other channel connected or
enabled - so each of those writes must be the guard's to allow. This scans the
application for them rather than trusting a list somebody keeps by hand:

* constructing a `WhatsAppAccount` (active by default) or a `ChannelConnection`
  other than with an explicitly non-active status;
* assigning `.status` an `ACTIVE` member, a status built from a value, or a
  value only known at run time, in a module that deals in connection statuses;
* SQL text that updates the status of `whatsapp_accounts` or
  `channel_connections`.

Each function holding one must either take the guard's `ChannelSlot` as a
parameter or call `reserve_or_refuse` itself. The set of writers is pinned too,
so a new one shows up here as a diff to be read, not as a silent new path.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

#: Every function that writes a connection active today, and why it is safe.
KNOWN_WRITERS: dict[tuple[str, str], str] = {
    ("repositories/whatsapp_repository.py", "connect"): "takes the guard's ChannelSlot",
    ("services/whatsapp_account_service.py", "set_status"): "reserve_or_refuse on enable",
    ("services/channel_connection_service.py", "connect"): "reserve_or_refuse before insert",
    ("services/channel_connection_service.py", "_write_status"): "takes the guard's ChannelSlot",
}

STATUS_TYPES = {"WhatsAppAccountStatus", "ConnectionStatus"}
CONNECTION_MODELS = {"WhatsAppAccount", "ChannelConnection"}
STATUS_SQL = re.compile(
    r"update\s+(whatsapp_accounts|channel_connections)\b.*\bstatus\b", re.IGNORECASE | re.DOTALL
)


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _activating_value(value: ast.AST) -> bool:
    """A status value that is, or may be, ACTIVE."""
    if isinstance(value, ast.Attribute) and _name(value.value) in STATUS_TYPES:
        return value.attr == "ACTIVE"
    if isinstance(value, ast.Call) and _name(value.func) in STATUS_TYPES:
        return True
    # A name, a parameter, a conditional: not provably inactive.
    return not isinstance(value, ast.Constant)


def _writes(function: ast.AST, *, statuses_in_scope: bool) -> list[str]:
    found: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _name(node.func) in CONNECTION_MODELS:
            status = next((k.value for k in node.keywords if k.arg == "status"), None)
            if status is None or _activating_value(status):
                found.append(f"constructs {_name(node.func)}")
        if statuses_in_scope and isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and _activating_value(node.value)
                ):
                    found.append("assigns .status")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and STATUS_SQL.search(node.value)
        ):
            found.append("updates a status in SQL")
    return found


def _guarded(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for argument in [*function.args.args, *function.args.kwonlyargs]:
        if argument.annotation is not None and "ChannelSlot" in ast.unparse(argument.annotation):
            return True
    return any(
        isinstance(node, ast.Call) and _name(node.func) == "reserve_or_refuse"
        for node in ast.walk(function)
    )


def _scan() -> dict[tuple[str, str], tuple[list[str], bool]]:
    writers: dict[tuple[str, str], tuple[list[str], bool]] = {}
    for path in APP.rglob("*.py"):
        if "models" in path.parts and path.parent.name == "models":
            # The model definitions themselves, not writers.
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        statuses_in_scope = any(name in source for name in STATUS_TYPES)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            found = _writes(node, statuses_in_scope=statuses_in_scope)
            if found:
                relative = path.relative_to(APP).as_posix()
                writers[(relative, node.name)] = (found, _guarded(node))
    return writers


def test_every_activation_writer_asks_the_guard() -> None:
    unguarded = {key: found for key, (found, guarded) in _scan().items() if not guarded}
    assert unguarded == {}, (
        "A function writes a channel connection active without the capacity guard. "
        "Take a ChannelSlot or call ChannelCapacityGuard.reserve_or_refuse (ENT-08)."
    )


def test_the_activation_writers_are_the_known_ones() -> None:
    """A new writer is a decision to read, not a path that appears unannounced."""
    assert set(_scan()) == set(KNOWN_WRITERS)


def test_the_scan_sees_an_unguarded_writer() -> None:
    """Non-vacuous: the scan flags exactly the shapes it claims to."""
    source = """
async def sneaky(account, status):
    account.status = WhatsAppAccountStatus.ACTIVE

async def built():
    return ChannelConnection(id=1, status=ConnectionStatus.ACTIVE)

async def raw(session):
    await session.execute(text("UPDATE channel_connections SET status = 'active'"))

async def harmless(account):
    account.status = WhatsAppAccountStatus.DISABLED
"""
    tree = ast.parse(source)
    flagged = {
        node.name: bool(_writes(node, statuses_in_scope=True))
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert flagged == {"sneaky": True, "built": True, "raw": True, "harmless": False}
