"""Meta errors are classified by Meta's code first (OMNI-035).

Meta's error-code reference documents throttling (4, 80007, 130429, 131056;
131057 during a throughput upgrade), refused credentials (0, 190) and
connection-level refusals (3, 10, 200-299, 131005, 368, 131031, 133010) without
promising an HTTP status for them (WhatsApp Cloud API "Error codes" and
"Throughput", read 2026-10-02). Every case below is answered with **HTTP 400**,
the status that used to make each of them a per-message decline.

Mutants this suite kills: M-O12 (130429 classified by status only) and, with the
sweep tests, M-O13 (a connection-level code treated as per-message).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.channels.outcomes import (
    ProviderAuthError,
    ProviderConnectionRefusedError,
    SendNotAttemptedError,
)
from app.core import telemetry
from app.core.exceptions import RateLimitedError
from app.integrations.meta.errors import MetaErrorClass, classify_meta_error
from app.integrations.whatsapp.client import WhatsAppClient

# ------------------------------------------------------------ the table


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, 4, MetaErrorClass.THROTTLED),
        (400, 80007, MetaErrorClass.THROTTLED),
        (400, 130429, MetaErrorClass.THROTTLED),
        (400, 131056, MetaErrorClass.THROTTLED),
        (400, 131057, MetaErrorClass.THROTTLED),
        (429, None, MetaErrorClass.THROTTLED),
        (400, 190, MetaErrorClass.CREDENTIAL),
        (401, None, MetaErrorClass.CREDENTIAL),
        (400, 0, MetaErrorClass.CREDENTIAL),
        (401, 10, MetaErrorClass.CREDENTIAL),
        (400, 3, MetaErrorClass.CONNECTION),
        (400, 10, MetaErrorClass.CONNECTION),
        (403, 200, MetaErrorClass.CONNECTION),
        (400, 299, MetaErrorClass.CONNECTION),
        (400, 131005, MetaErrorClass.CONNECTION),
        (400, 368, MetaErrorClass.CONNECTION),
        (400, 131031, MetaErrorClass.CONNECTION),
        (400, 133010, MetaErrorClass.CONNECTION),
        (400, 131047, MetaErrorClass.PER_MESSAGE),
        (400, 100, MetaErrorClass.PER_MESSAGE),
        (403, None, MetaErrorClass.PER_MESSAGE),
        (400, None, MetaErrorClass.PER_MESSAGE),
    ],
)
def test_the_code_decides_and_the_status_is_the_fallback(
    status: int, code: int | None, expected: MetaErrorClass
) -> None:
    assert classify_meta_error(status, code) is expected


def test_the_metric_domain_is_the_class_vocabulary() -> None:
    assert frozenset(member.value for member in MetaErrorClass) == (
        telemetry.PROVIDER_ERROR_CLASSES
    )
    assert telemetry.REDIS_COUNTERS["wasla_provider_errors_total"][1] == ("provider", "class")


# ----------------------------------------------------- the client contract


class Graph:
    """Meta's messages endpoint answering every request with one error."""

    def __init__(self, status: int, code: int | None) -> None:
        self.status = status
        self.code = code
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            error: dict[str, Any] = {"message": "(Meta's words)", "type": "OAuthException"}
            if self.code is not None:
                error["code"] = self.code
            return httpx.Response(self.status, content=json.dumps({"error": error}).encode())

        return httpx.MockTransport(handle)


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    seen: list[tuple[str, dict[str, str]]] = []

    async def record(metric: str, labels: dict[str, str], amount: int) -> None:
        seen.append((metric, dict(labels)))

    monkeypatch.setattr(telemetry, "_increment_by", record)
    return seen


async def _send(graph: Graph) -> None:
    async def no_wait(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=graph.transport()) as http:
        client = WhatsAppClient(http=http, access_token="t", api_version="v21.0", sleep=no_wait)
        await client.send_text(phone_number_id="PN", to="201000000001", body="hello")


def _classes(counted: list[tuple[str, dict[str, str]]]) -> list[str]:
    return [
        labels["class"] for metric, labels in counted if metric == "wasla_provider_errors_total"
    ]


@pytest.mark.parametrize("code", [130429, 131056, 4, 80007, 131057])
async def test_a_throttling_code_on_a_400_is_a_rate_limit_retried_first(
    code: int, counted: list[tuple[str, dict[str, str]]]
) -> None:
    graph = Graph(400, code)

    with pytest.raises(RateLimitedError):
        await _send(graph)

    # Retried like a 429 - declined before reading, so a retry duplicates nothing.
    assert graph.calls == 3
    assert _classes(counted) == ["throttled"]


async def test_a_429_without_a_code_is_still_a_rate_limit(
    counted: list[tuple[str, dict[str, str]]],
) -> None:
    graph = Graph(429, None)

    with pytest.raises(RateLimitedError):
        await _send(graph)

    assert graph.calls == 3


@pytest.mark.parametrize("code", [10, 131031, 133010, 368, 200])
async def test_a_connection_level_code_is_the_connections_refusal(
    code: int, counted: list[tuple[str, dict[str, str]]]
) -> None:
    graph = Graph(400, code)

    with pytest.raises(ProviderConnectionRefusedError) as refused:
        await _send(graph)

    assert not isinstance(refused.value, ProviderAuthError)
    assert refused.value.health == "permission_missing"
    assert refused.value.reason == f"meta_code_{code}"
    assert "(Meta's words)" not in str(refused.value)
    assert graph.calls == 1
    assert _classes(counted) == ["connection"]


async def test_code_190_keeps_its_credential_meaning(
    counted: list[tuple[str, dict[str, str]]],
) -> None:
    with pytest.raises(ProviderAuthError) as refused:
        await _send(Graph(400, 190))

    assert refused.value.health == "auth_failed"
    assert _classes(counted) == ["credential"]


async def test_an_ordinary_decline_stays_this_messages(
    counted: list[tuple[str, dict[str, str]]],
) -> None:
    with pytest.raises(SendNotAttemptedError) as refused:
        await _send(Graph(400, 131047))

    assert not isinstance(refused.value, ProviderConnectionRefusedError)
    assert _classes(counted) == ["per_message"]
