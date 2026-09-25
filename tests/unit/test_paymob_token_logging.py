"""No log line carries a card token, a payment token or an auth token (B24).

The audit's mutation B24 wrote the saved card token into
`billing.paymob_saved_method_charged` and nothing noticed: no test captured the
adapter's logs. These drive the real provider through a saved-card charge and a
Card Token Inquiry against a mock transport, with every bearer value a
sentinel, and search every captured record - message, arguments and every
extra field - for any of them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from app.integrations.billing.checkout import SavedMethodCharge
from app.integrations.billing.paymob import (
    CARD_TOKEN_INQUIRY_PATH,
    INQUIRY_AUTH_PATH,
    INTENTION_PATH,
    PAY_PATH,
    PaymobProvider,
)

CARD_TOKEN = "SENTINEL-card-token-7f3a"
PAYMENT_KEY = "SENTINEL-payment-key-91be"
AUTH_TOKEN = "SENTINEL-auth-token-c04d"
SECRET_KEY = "sk_test_SENTINEL-secret-5e21"
API_KEY = "SENTINEL-api-key-aa18"
HMAC_SECRET = "SENTINEL-hmac-6d0c"
SENTINELS = (CARD_TOKEN, PAYMENT_KEY, AUTH_TOKEN, SECRET_KEY, API_KEY, HMAC_SECRET)
ORDER_ID = 424242


def _paymob(handler: Callable[[httpx.Request], httpx.Response]) -> PaymobProvider:
    return PaymobProvider(
        secret_key=SECRET_KEY,
        public_key="pk_test_notreal",
        hmac_secret=HMAC_SECRET,
        integration_ids=[4097558],
        moto_integration_id=5934829,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
    )


def _answers(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith(INTENTION_PATH):
        return httpx.Response(
            201,
            json={
                "id": "pi_test_logging",
                "intention_order_id": ORDER_ID,
                "client_secret": "egy_csk_test_x",
                "payment_keys": [{"key": PAYMENT_KEY, "integration": 5934829}],
            },
        )
    if path.endswith(PAY_PATH):
        return httpx.Response(200, json={"id": 9001, "pending": True})
    if path.endswith(INQUIRY_AUTH_PATH):
        return httpx.Response(201, json={"token": AUTH_TOKEN})
    if path.endswith(CARD_TOKEN_INQUIRY_PATH):
        return httpx.Response(
            200,
            json=[
                {
                    "type": "TOKEN",
                    "obj": {
                        "id": 55,
                        "token": CARD_TOKEN,
                        "masked_pan": "xxxx-xxxx-xxxx-2346",
                        "card_subtype": "MasterCard",
                        "order_id": ORDER_ID,
                    },
                }
            ],
        )
    return httpx.Response(404, json={})


def _leaks(records: list[logging.LogRecord]) -> list[str]:
    found: list[str] = []
    for record in records:
        rendered = (
            json.dumps({key: repr(value) for key, value in vars(record).items()}, default=repr)
            + record.getMessage()
        )
        found.extend(f"{record.name}:{s}" for s in SENTINELS if s in rendered)
    return found


async def test_a_saved_card_charge_logs_no_bearer_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _paymob(_answers)
    with caplog.at_level(logging.DEBUG):
        reference = await provider.charge_saved_method(
            SavedMethodCharge(
                reference="renewal-ref",
                token=CARD_TOKEN,
                amount=Decimal("99.00"),
                currency="EGP",
                description="Pro renewal",
                customer_email="owner@example.com",
                customer_name="Owner Person",
            )
        )

    assert reference == "9001"
    # The charge was logged, so the absence below is about content, not silence.
    assert any("saved_method_charged" in r.getMessage() for r in caplog.records)
    assert _leaks(caplog.records) == []


async def test_a_card_token_inquiry_logs_no_bearer_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _paymob(_answers)
    with caplog.at_level(logging.DEBUG):
        method = await provider.inquire_saved_method(str(ORDER_ID))

    assert method is not None and method.token == CARD_TOKEN
    assert _leaks(caplog.records) == []


async def test_the_sentinel_search_would_see_a_leak(caplog: pytest.LogCaptureFixture) -> None:
    """The control: the same search over a record that does carry a token."""
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.integrations.billing.paymob").info(
            "billing.paymob_saved_method_charged", extra={"token": CARD_TOKEN}
        )
    assert _leaks(caplog.records) != []


async def test_an_order_without_a_saved_card_is_no_card_not_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Paymob's real answer for an order nobody saved a card on (Test API, 2026-09-25)."""

    def answers(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(CARD_TOKEN_INQUIRY_PATH):
            return httpx.Response(404, json={"message": "No card tokens found for this order"})
        return _answers(request)

    with caplog.at_level(logging.DEBUG):
        method = await _paymob(answers).inquire_saved_method(str(ORDER_ID))

    assert method is None
    assert not any("card_token_inquiry_failed" in r.getMessage() for r in caplog.records)
