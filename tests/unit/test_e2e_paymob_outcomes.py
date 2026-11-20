"""The fake Paymob page's outcomes (EC8-04): what each sends, and that the
redirect says success=true whatever happened.

A real redirect carries `success=true` while the payment is still pending, and
a decline's redirect can say the same (P02, P54); the return page is proved
against exactly that, so the fake must reproduce it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.e2e_stack import fakes

SECRET = "egy_csk_test_outcomes"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, Any], float]]:
    calls: list[tuple[dict[str, Any], float]] = []

    async def record(transaction: dict[str, Any], *, delay: float = 0.0) -> None:
        calls.append((transaction, delay))

    monkeypatch.setattr(fakes, "_paymob_callback", record)
    monkeypatch.setitem(
        fakes._intentions,
        SECRET,
        {
            "special_reference": "wasla-ref-1",
            "amount": 9900,
            "currency": "EGP",
            "payment_methods": [4097558],
            "redirection_url": "http://localhost:3000/billing/checkout/return",
        },
    )
    return calls


def _open(outcome: str | None, **extra: str) -> Any:
    query = {"clientSecret": SECRET, **({"outcome": outcome} if outcome else {}), **extra}
    with TestClient(fakes.service()) as client:
        response = client.get("/paymob/", params=query, follow_redirects=False)
    return response


def _redirect(response: Any) -> dict[str, list[str]]:
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert target.path == "/billing/checkout/return"
    return parse_qs(target.query)


def test_paid_sends_the_success_callback_then_redirects(sent: list[Any]) -> None:
    query = _redirect(_open(None))
    assert query["success"] == ["true"] and query["pending"] == ["false"]
    ((transaction, delay),) = sent
    assert transaction["success"] is True and delay == 0.0


def test_declined_sends_a_failure_while_the_redirect_still_says_success(
    sent: list[Any],
) -> None:
    query = _redirect(_open("declined"))
    assert query["success"] == ["true"]
    ((transaction, _),) = sent
    assert transaction["success"] is False
    assert transaction["data"]["message"] == "AUTHENTICATION_FAILED"


def test_pending_sends_no_callback(sent: list[Any]) -> None:
    query = _redirect(_open("pending"))
    assert query["success"] == ["true"] and query["pending"] == ["true"]
    assert sent == []


def test_late_schedules_the_success_callback_and_bounds_its_delay(sent: list[Any]) -> None:
    query = _redirect(_open("late", delay="999"))
    assert query["success"] == ["true"]
    ((transaction, delay),) = sent
    assert transaction["success"] is True and delay == 30.0


def test_an_unknown_outcome_is_refused(sent: list[Any]) -> None:
    assert _open("refunded").status_code == 400
    assert sent == []


def test_the_api_never_sees_the_outcome() -> None:
    """The switch is the fake's: no application module names it."""
    from pathlib import Path

    app = Path(__file__).resolve().parents[2] / "app"
    assert not [path for path in app.rglob("*.py") if "PAYMOB_OUTCOMES" in path.read_text("utf-8")]
    assert asyncio.iscoroutinefunction(fakes._paymob_callback)
