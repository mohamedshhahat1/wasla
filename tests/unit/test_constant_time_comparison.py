"""Authenticity values are compared in constant time (audit S04 / S04b).

This is a **structural** guarantee, and it is pinned structurally on purpose.
Whether a comparison leaks timing cannot be measured reliably in CI - a
nanosecond benchmark on a shared runner is noise - so the audit's mutation
swapping `compare_digest` for `==` survived every functional test, correctly:
the answers are identical either way.

What can be asserted is the shape of the code: every function that decides
whether a caller-supplied secret is genuine goes through
`app.core.secure_compare.secrets_match`, that helper goes through
`hmac.compare_digest`, and none of those functions compares a secret with
`==` or `!=`. A refactor that quietly replaces one fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.secure_compare import secrets_match

ROOT = Path(__file__).resolve().parents[2]

# Every function that decides whether a caller-supplied secret is genuine,
# by module. A new verifier belongs in this table.
VERIFIERS: dict[str, tuple[str, ...]] = {
    "app/integrations/whatsapp/signature.py": ("verify_signature",),
    "app/api/v1/webhooks.py": ("verify_subscription",),
    "app/integrations/email/signature.py": ("verify_signature",),
    "app/integrations/billing/paymob.py": ("verify_callback", "verify_token_callback"),
    "app/core/oauth_binding.py": ("matches",),
    "app/integrations/google/oidc.py": ("_check_nonce",),
}

# Names that hold, in these functions, either the secret the caller sent or
# the one it is checked against. Comparing any of them with `==` is the defect.
SECRET_NAMES = frozenset(
    {"expected", "signature", "header", "token", "value", "presented", "digest", "secret"}
)


def _functions(module: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _called(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


CASES = [(module, name) for module, names in VERIFIERS.items() for name in names]


@pytest.mark.parametrize(("module", "function"), CASES)
def test_the_verifier_exists(module: str, function: str) -> None:
    """Non-vacuity: a renamed verifier must not silently leave this table."""
    assert function in _functions(module)


@pytest.mark.parametrize(("module", "function"), CASES)
def test_every_verifier_compares_through_the_constant_time_helper(
    module: str, function: str
) -> None:
    assert "secrets_match" in _called(_functions(module)[function])


@pytest.mark.parametrize(("module", "function"), CASES)
def test_no_verifier_compares_a_secret_with_equality(module: str, function: str) -> None:
    for node in ast.walk(_functions(module)[function]):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.Eq | ast.NotEq) for op in node.ops
        ):
            operands = [node.left, *node.comparators]
            leaked = set().union(*(_names(operand) for operand in operands)) & SECRET_NAMES
            assert not leaked, f"{module}:{function} compares {leaked} with == or !="


def test_no_authenticity_module_calls_compare_digest_on_strings_directly() -> None:
    """The helper is the one place `compare_digest` is called with caller
    input; a direct `compare_digest(str, str)` is SEC-03 coming back."""
    for module in VERIFIERS:
        assert "compare_digest" not in _called(ast.parse((ROOT / module).read_text("utf-8")))


def test_the_helper_is_built_on_compare_digest_over_bytes() -> None:
    helper = _functions("app/core/secure_compare.py")["secrets_match"]
    calls = [
        call
        for call in ast.walk(helper)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "compare_digest"
    ]

    assert calls, "secrets_match no longer uses hmac.compare_digest"
    for node in ast.walk(helper):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.Eq | ast.NotEq) for op in node.ops
        ):
            names = set().union(*(_names(x) for x in [node.left, *node.comparators]))
            assert not names & {"ours", "theirs", "expected", "supplied"}


@pytest.mark.parametrize(
    ("expected", "supplied", "result"),
    [
        ("abc", "abc", True),
        ("abc", "abd", False),
        ("abc", "ab", False),
        ("abc", None, False),
        ("abc", "", False),
        ("abc", "é", False),
        ("abc", "توقيع", False),
        ("abc", "🔑", False),
        ("abc", "\ud800", False),  # a lone surrogate cannot be encoded at all
        ("مفتاح", "مفتاح", True),
    ],
)
def test_the_helper_answers_and_never_raises(
    expected: str, supplied: str | None, result: bool
) -> None:
    assert secrets_match(expected, supplied) is result
