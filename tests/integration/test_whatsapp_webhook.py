"""The webhook HTTP surface.

Ingestion is stubbed here; what matters at this level is that an unsigned
request never reaches it, and that Meta always gets an answer it will not
retry forever.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.api.v1.webhooks import get_ingestion_service
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.services.whatsapp_service import IngestionOutcome

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "meta-app-secret"
VERIFY_TOKEN = "meta-verify-token"
PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"phone_number_id": "109876543210"},
                        "messages": [
                            {"from": "2012", "id": "wamid.one", "type": "text"},
                        ],
                    }
                }
            ]
        }
    ],
}


class StubIngestion:
    def __init__(self):
        self.calls = []

    async def ingest(self, payload):
        self.calls.append(payload)
        return IngestionOutcome(stored=1)


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
def configured(app) -> Settings:
    settings = Settings(
        _env_file=None,
        environment="test",
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
    )
    app.dependency_overrides[get_settings_from_state] = lambda: settings
    return settings


@pytest.fixture
def ingestion(app) -> StubIngestion:
    stub = StubIngestion()
    app.dependency_overrides[get_ingestion_service] = lambda: stub
    return stub


async def test_verification_echoes_the_challenge_to_a_caller_with_the_token(client, configured):
    response = await client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_verification_with_the_wrong_token_never_echoes_the_challenge(client, configured):
    response = await client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "guessed",
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 403
    assert "1158201444" not in response.text


async def test_verification_requires_the_subscribe_mode(client, configured):
    response = await client.get(
        PATH,
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 403


async def test_a_signed_delivery_is_ingested(client, configured, ingestion):
    body = json.dumps(PAYLOAD).encode()

    response = await client.post(PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert ingestion.calls == [PAYLOAD]


async def test_an_unsigned_delivery_never_reaches_ingestion(client, configured, ingestion):
    body = json.dumps(PAYLOAD).encode()

    response = await client.post(PATH, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 403
    assert ingestion.calls == []


async def test_a_forged_signature_never_reaches_ingestion(client, configured, ingestion):
    body = json.dumps(PAYLOAD).encode()
    forged = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()

    response = await client.post(
        PATH,
        content=body,
        headers={"X-Hub-Signature-256": f"sha256={forged}"},
    )

    assert response.status_code == 403
    assert ingestion.calls == []


async def test_a_signature_for_a_different_body_is_refused(client, configured, ingestion):
    headers = _signed(b'{"object":"something else"}')

    response = await client.post(PATH, content=json.dumps(PAYLOAD).encode(), headers=headers)

    assert response.status_code == 403
    assert ingestion.calls == []


async def test_an_unparseable_body_is_acknowledged_rather_than_retried(
    client, configured, ingestion
):
    body = b"not json at all"

    response = await client.post(PATH, content=body, headers=_signed(body))

    # A 4xx here would have Meta retry a payload that can never parse.
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert ingestion.calls == []


async def test_a_json_body_that_is_not_an_object_is_acknowledged(client, configured, ingestion):
    body = b"[1, 2, 3]"

    response = await client.post(PATH, content=body, headers=_signed(body))

    assert response.status_code == 200
    assert ingestion.calls == []
