"""Malformed authenticity values fail as authentication failures (SEC-03).

`hmac.compare_digest(str, str)` raises on non-ASCII input, and every public
callback compared a caller-chosen value that way. The audit sent `é` to each of
them and got a 500 back: still a refusal, but an *unhandled* one - counted as
`wasla_unhandled_errors_total`, paging through the 5xx alert, and missing from
the `invalid_signature` series that exists to show forgery attempts.

Each case below asserts four things for every public authenticity check:

* the answer is the endpoint's ordinary refusal (403), never a 5xx;
* nothing downstream of verification ran;
* the refusal was counted as a refusal and not as an unhandled error;
* the value the caller supplied never reached a log line.

Header values are sent as UTF-8 bytes; a server decodes headers as Latin-1, so
each still arrives as a non-ASCII string - which is the input that crashed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.v1.webhooks import get_ingestion_service
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.telemetry import AUTH_SECURITY_EVENTS, UNHANDLED_ERRORS

pytestmark = pytest.mark.integration

APP_SECRET = "malformed-values-meta-app-secret"
VERIFY_TOKEN = "malformed-values-verify-token"
RESEND_SECRET = "whsec_" + "c2VjcmV0LXZhbHVlLWZvci10ZXN0aW5nLW9ubHkh"
PAYMOB_SECRET = "malformed-values-paymob-hmac-secret"

MALFORMED = {
    "latin1": "é",
    "arabic": "توقيع-مزوّر-للاختبار",
    "emoji": "🔑🔐forged-signature",
    "invalid_hex": "zz" * 64,
    "very_long": "A" * 8192 + "é",
    "empty": "",
}
# Distinctive enough that finding one in a log line means it was logged.
LOGGABLE = {name: value for name, value in MALFORMED.items() if len(value) >= 8}


class _Refusing:
    """Stands in for everything that may only run after verification."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def ingest(self, payload: Any) -> None:
        self.calls.append(payload)


@pytest.fixture
def downstream(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Refusing]:
    settings = Settings(
        _env_file=None,
        environment="test",
        log_format="json",
        log_level="DEBUG",
        cors_origins=[],
        rate_limit_enabled=False,
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        resend_webhook_secret=RESEND_SECRET,
        billing_provider="paymob",
        paymob_secret_key="sk_test_notreal",
        paymob_public_key="pk_test_notreal",
        paymob_hmac_secret=PAYMOB_SECRET,
        paymob_integration_ids=[4097558],
    )
    spy = _Refusing()
    app.dependency_overrides[get_settings_from_state] = lambda: settings
    app.dependency_overrides[get_ingestion_service] = lambda: spy

    import app.api.v1.email_webhooks as email_route
    import app.api.v1.payment_webhooks as payment_route

    class _Service:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            spy.calls.append(("constructed", type(self).__name__))

        async def record(self, *args: Any, **kwargs: Any) -> None:
            spy.calls.append("record")

    async def _remember(*args: Any, **kwargs: Any) -> None:
        spy.calls.append("remember_saved_method")

    monkeypatch.setattr(email_route, "EmailEventService", _Service)
    monkeypatch.setattr(payment_route, "CheckoutService", _Service)
    monkeypatch.setattr(payment_route, "remember_saved_method", _remember)
    yield spy
    app.dependency_overrides.clear()


def _blocked(event: str, reason: str = "invalid_signature") -> float:
    return AUTH_SECURITY_EVENTS.value(event=event, outcome="blocked", reason=reason)


async def _assert_refused(
    send: Any,
    *,
    downstream: _Refusing,
    event: str,
    reason: str,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocked, crashed = _blocked(event, reason), UNHANDLED_ERRORS.value()
    caplog.set_level(logging.DEBUG)

    response = await send()

    assert response.status_code == 403, response.text
    assert downstream.calls == []
    assert _blocked(event, reason) == blocked + 1
    assert UNHANDLED_ERRORS.value() == crashed
    if len(value) >= 8:
        for record in caplog.records:
            rendered = record.getMessage() + json.dumps(record.__dict__, default=str)
            assert value not in rendered, f"{event}: the supplied value was logged"


@pytest.mark.parametrize("value", MALFORMED.values(), ids=MALFORMED.keys())
async def test_meta_delivery_signature(
    client: AsyncClient,
    downstream: _Refusing,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def send() -> Any:
        return await client.post(
            "/api/v1/webhooks/whatsapp",
            content=b'{"object":"whatsapp_business_account","entry":[]}',
            headers={
                b"content-type": b"application/json",
                b"x-hub-signature-256": f"sha256={value}".encode(),
            },
        )

    await _assert_refused(
        send,
        downstream=downstream,
        event="whatsapp_webhook",
        reason="invalid_signature",
        value=value,
        caplog=caplog,
    )


@pytest.mark.parametrize("value", MALFORMED.values(), ids=MALFORMED.keys())
async def test_meta_verify_token(
    client: AsyncClient,
    downstream: _Refusing,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def send() -> Any:
        return await client.get(
            "/api/v1/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": value,
                "hub.challenge": "1158201444",
            },
        )

    await _assert_refused(
        send,
        downstream=downstream,
        event="whatsapp_verification",
        reason="invalid_token",
        value=value,
        caplog=caplog,
    )


@pytest.mark.parametrize("value", MALFORMED.values(), ids=MALFORMED.keys())
async def test_resend_svix_signature(
    client: AsyncClient,
    downstream: _Refusing,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import time

    async def send() -> Any:
        return await client.post(
            "/api/v1/webhooks/email",
            content=b'{"type":"email.delivered","data":{}}',
            headers={
                b"content-type": b"application/json",
                b"svix-id": b"msg_malformed",
                b"svix-timestamp": str(int(time.time())).encode(),
                b"svix-signature": f"v1,{value}".encode(),
            },
        )

    await _assert_refused(
        send,
        downstream=downstream,
        event="email_webhook",
        reason="invalid_signature",
        value=value,
        caplog=caplog,
    )


@pytest.mark.parametrize("kind", ["TRANSACTION", "TOKEN"])
@pytest.mark.parametrize("value", MALFORMED.values(), ids=MALFORMED.keys())
async def test_paymob_hmac(
    client: AsyncClient,
    downstream: _Refusing,
    value: str,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"type": kind, "obj": {"id": 1, "token": "synthetic-card-token", "order": {"id": 1}}}

    async def send() -> Any:
        return await client.post(
            "/api/v1/webhooks/paymob",
            params={"hmac": value},
            content=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )

    await _assert_refused(
        send,
        downstream=downstream,
        event="paymob_webhook",
        reason="invalid_signature",
        value=value,
        caplog=caplog,
    )
