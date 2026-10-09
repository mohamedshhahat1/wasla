"""Meta-sized webhook deliveries are accepted; any refusal is counted (OMNI-034).

Meta documents webhook payloads "up to 3 MB" and retries a refused delivery for
up to seven days - identically, so a delivery the size limit refuses once is
refused for ever and its messages never arrive. The webhook cap was 1 MiB, and
well-formed signed deliveries of 1.47 MB and 2.57 MB (long Arabic replies, two
bytes a character) were answered 413 with nothing but a log line.

Every case goes through the real application - the size middleware, the
signature check and the route - with a correctly signed body; the ingestion
service is the only stand-in, so what is proved is that the bytes reach it.

Mutants this suite kills: M-O08 (the 1 MiB cap restored) and M-O09 (the refusal
counter removed).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.v1.webhooks import get_ingestion_service
from app.core import telemetry
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.services.whatsapp_service import IngestionOutcome

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "body-cap-app-secret"
MIB = 1024 * 1024
ARABIC = "مرحبا، أريد أن أعرف تفاصيل العرض والأسعار والتوصيل من فضلك. " * 60


class RecordingIngestion:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def ingest(self, payload: dict[str, Any]) -> IngestionOutcome:
        self.payloads.append(payload)
        return IngestionOutcome(stored=len(payload["entry"][0]["changes"][0]["value"]["messages"]))


@pytest.fixture
def ingestion(app: FastAPI) -> RecordingIngestion:
    configured = Settings(
        _env_file=None, environment="test", meta_app_secret=APP_SECRET, meta_verify_token="v"
    )
    app.dependency_overrides[get_settings_from_state] = lambda: configured
    recorder = RecordingIngestion()
    app.dependency_overrides[get_ingestion_service] = lambda: recorder
    return recorder


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    seen: list[tuple[str, dict[str, str]]] = []

    async def record(metric: str, labels: dict[str, str], amount: int) -> None:
        seen.append((metric, dict(labels)))

    monkeypatch.setattr(telemetry, "_increment_by", record)
    return seen


def _delivery(at_least: int) -> bytes:
    """A well-formed WhatsApp delivery of long Arabic messages, at least this many bytes."""
    messages: list[dict[str, Any]] = []
    while True:
        messages.append(
            {
                "from": "201000000777",
                "id": f"wamid.{uuid.uuid4().hex}",
                "timestamp": "1790000000",
                "type": "text",
                "text": {"body": ARABIC},
            }
        )
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "pn-cap"},
                                "messages": messages,
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) >= at_least:
            return body


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _refusals(counted: list[tuple[str, dict[str, str]]]) -> list[dict[str, str]]:
    return [labels for metric, labels in counted if metric == "wasla_http_body_too_large_total"]


@pytest.mark.parametrize("size", [1_468_122, 2_568_972, 2_900_000])
async def test_a_meta_sized_signed_delivery_reaches_ingestion(
    client: AsyncClient,
    ingestion: RecordingIngestion,
    counted: list[tuple[str, dict[str, str]]],
    size: int,
) -> None:
    body = _delivery(size)
    # Non-vacuity: larger than the old cap, and within Meta's documented 3 MB.
    assert MIB < len(body) <= 3 * 1_000_000

    response = await client.post(PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    (received,) = ingestion.payloads
    assert received == json.loads(body)
    assert _refusals(counted) == []


async def test_a_delivery_beyond_metas_maximum_is_refused_and_counted(
    client: AsyncClient,
    ingestion: RecordingIngestion,
    counted: list[tuple[str, dict[str, str]]],
) -> None:
    body = _delivery(3_500_000)

    response = await client.post(PATH, content=body, headers=_signed(body))

    assert response.status_code == 413
    assert ingestion.payloads == []
    assert _refusals(counted) == [{"route_group": "webhook"}]


async def test_a_streamed_oversized_delivery_is_counted_the_same_way(
    client: AsyncClient,
    ingestion: RecordingIngestion,
    counted: list[tuple[str, dict[str, str]]],
) -> None:
    """No Content-Length: cut off as it streams, and still counted."""
    body = _delivery(3_500_000)

    async def chunks() -> Any:
        for start in range(0, len(body), 256 * 1024):
            yield body[start : start + 256 * 1024]

    response = await client.post(PATH, content=chunks(), headers=_signed(body))

    assert response.status_code == 413
    assert ingestion.payloads == []
    assert _refusals(counted) == [{"route_group": "webhook"}]


async def test_an_oversized_api_request_is_counted_apart_from_the_webhook(
    client: AsyncClient, counted: list[tuple[str, dict[str, str]]]
) -> None:
    """The label is a closed group, never the path a caller chose."""
    response = await client.post(
        "/api/v1/auth/login",
        content=b"{" + b" " * (200 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert _refusals(counted) == [{"route_group": "api"}]
