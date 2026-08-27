"""The Paymob callback signature, pinned to the vendor's own worked example.

This is the file that decides whether the webhook endpoint is a security
control or a formality, and it is worth saying why it is shaped this way.

Signature verification cannot be tested against itself. A test that signs a
payload with our own function and then verifies it with our own function
passes for *any* consistent field order, including a wrong one - and a wrong
order is not discovered until a live callback from a real merchant account
fails to verify, at which point the tempting fix is to stop verifying.

So the anchor here is external: the documentation publishes a sample
transaction and the exact string that must be built from it
(https://developers.paymob.com/paymob-docs/developers/webhook-callbacks-and-hmac/hmac/hmac-transaction-callback,
last updated 1 June 2026, read 27 August 2026). `EXPECTED_MESSAGE` below is
that string copied verbatim. If a future edit reorders a field, drops one, or
formats a boolean as Python spells it, this fails immediately and says so.

The documentation's *digest* for that string is not asserted, and that gap is
deliberate rather than overlooked: reproducing it needs the HMAC secret the
docs used, which they do not publish. The digest is a pure function of the
message and the secret, so pinning the message pins the part that is ours to
get wrong; the remaining half is `hmac.new(..., sha512).hexdigest()`, which is
the standard library's to get right.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
from decimal import Decimal

import pytest

from app.db.models.invoice import PaymentStatus
from app.integrations.billing.checkout import CallbackVerificationError
from app.integrations.billing.paymob import (
    HMAC_FIELDS,
    PaymobProvider,
    hmac_message,
    hmac_signature,
)

# The transaction object from the documentation's worked example, trimmed to
# the fields the signature covers plus the two the reader needs to follow it.
# Trimmed rather than pasted whole because the published sample is 200 lines of
# acquirer detail, none of which is signed.
DOCUMENTED_TRANSACTION = {
    "id": 192036465,
    "pending": False,
    "amount_cents": 100000,
    "success": True,
    "is_auth": False,
    "is_capture": False,
    "is_standalone_payment": True,
    "is_voided": False,
    "is_refunded": False,
    "is_3d_secure": True,
    "integration_id": 4097558,
    "has_parent_transaction": False,
    "order": {"id": 217503754, "merchant_order_id": None},
    "created_at": "2024-06-13T11:33:44.592345",
    "currency": "EGP",
    "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
    "error_occured": False,
    "owner": 302852,
}

# Copied verbatim from the documentation's "HMAC Concatenated String" panel.
EXPECTED_MESSAGE = (
    "1000002024-06-13T11:33:44.592345EGPfalsefalse1920364654097558"
    "truefalsefalsefalsetruefalse217503754302852false2346MasterCardcardtrue"
)

SECRET = "a-test-hmac-secret"


def _provider(**overrides) -> PaymobProvider:
    settings = {
        "secret_key": "sk_test_notreal",
        "public_key": "pk_test_notreal",
        "hmac_secret": SECRET,
        "integration_ids": [4097558],
    }
    settings.update(overrides)
    return PaymobProvider(**settings)  # type: ignore[arg-type]


def _signed(transaction: dict, *, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps({"type": "TRANSACTION", "obj": transaction}).encode("utf-8")
    return body, hmac_signature(transaction, secret=secret)


# ------------------------------------------------------- the documented vector


def test_the_signed_string_matches_the_documented_example() -> None:
    """The whole point of this file.

    A reordered field, a dropped field, or `True` instead of `true` all fail
    here rather than against a live merchant account.
    """
    assert hmac_message(DOCUMENTED_TRANSACTION) == EXPECTED_MESSAGE


def test_twenty_fields_are_signed_in_the_documented_order() -> None:
    """The count is part of the contract: Paymob lists exactly twenty keys."""
    assert len(HMAC_FIELDS) == 20
    assert HMAC_FIELDS[0] == "amount_cents"
    assert HMAC_FIELDS[-1] == "success"
    # The two nested ones, which are where a naive lexicographic sort of these
    # strings would put them in the wrong place.
    assert HMAC_FIELDS[5] == "id"
    assert HMAC_FIELDS[13] == "order.id"


def test_the_digest_is_hmac_sha512_hex() -> None:
    """Pinned against the standard library rather than against ourselves."""
    expected = hmac_module.new(
        SECRET.encode("utf-8"),
        EXPECTED_MESSAGE.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()

    assert hmac_signature(DOCUMENTED_TRANSACTION, secret=SECRET) == expected
    assert len(expected) == 128


def test_booleans_are_json_spelled_not_python_spelled() -> None:
    """`str(True)` is `True`, and every signature built that way fails."""
    message = hmac_message(DOCUMENTED_TRANSACTION)

    assert "true" in message
    assert "false" in message
    assert "True" not in message
    assert "False" not in message


def test_a_missing_field_contributes_nothing_rather_than_a_default() -> None:
    """Guessing a default would be inventing the bytes being authenticated."""
    without_pan = json.loads(json.dumps(DOCUMENTED_TRANSACTION))
    without_pan["source_data"].pop("pan")

    assert hmac_message(without_pan) == EXPECTED_MESSAGE.replace("2346", "", 1)


# ------------------------------------------------------------- verification


def test_a_correctly_signed_callback_is_accepted() -> None:
    body, signature = _signed(DOCUMENTED_TRANSACTION)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.status is PaymentStatus.SUCCEEDED
    assert event.event_id == "192036465"
    assert event.amount == Decimal("1000.00")
    assert event.currency == "EGP"


def test_a_tampered_amount_is_refused() -> None:
    """The signature covers `amount_cents`, so raising it breaks the digest.

    This is the attack the HMAC exists to stop: a forged callback claiming a
    large payment against a real order.
    """
    body, signature = _signed(DOCUMENTED_TRANSACTION)
    tampered = json.loads(body)
    tampered["obj"]["amount_cents"] = 1

    with pytest.raises(CallbackVerificationError):
        _provider().verify_callback(
            payload=json.dumps(tampered).encode("utf-8"),
            signature=signature,
        )


def test_a_signature_from_a_different_secret_is_refused() -> None:
    body, signature = _signed(DOCUMENTED_TRANSACTION, secret="somebody-elses-secret")

    with pytest.raises(CallbackVerificationError):
        _provider().verify_callback(payload=body, signature=signature)


@pytest.mark.parametrize(
    "signature",
    [None, "", "not-hex", "0" * 128],
)
def test_a_missing_or_wrong_signature_is_refused(signature: str | None) -> None:
    body, _ = _signed(DOCUMENTED_TRANSACTION)

    with pytest.raises(CallbackVerificationError):
        _provider().verify_callback(payload=body, signature=signature)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json at all",
        b"[]",
        b'"a string"',
        b"{}",
        b'{"type": "TRANSACTION"}',
        b'{"type": "TRANSACTION", "obj": null}',
        b'{"type": "TRANSACTION", "obj": "not an object"}',
    ],
)
def test_a_malformed_body_is_refused_rather_than_parsed_around(payload: bytes) -> None:
    """Fail closed. There is no branch returning an event for input like this."""
    with pytest.raises(CallbackVerificationError):
        _provider().verify_callback(payload=payload, signature="0" * 128)


def test_verification_uses_a_constant_time_comparison() -> None:
    """Asserted by reading the module, because timing cannot be measured here.

    A byte-at-a-time equality against a 128-character hex digest is a practical
    oracle over enough requests, and `==` on two strings is exactly that. This
    pins the call so a refactor to `expected == signature` fails.
    """
    source = __import__("inspect").getsource(PaymobProvider.verify_callback)
    assert "compare_digest" in source
    assert "expected == signature" not in source


# ----------------------------------------------------------- status mapping


def test_a_success_that_is_still_pending_is_not_a_success() -> None:
    """The mapping that decides whether a subscription activates.

    `success` true alongside `pending` true is a transaction in flight. Reading
    only `success` would activate a subscription for money that has not moved.
    """
    in_flight = {**DOCUMENTED_TRANSACTION, "success": True, "pending": True}
    body, signature = _signed(in_flight)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.status is PaymentStatus.PENDING
    assert not event.succeeded


def test_an_errored_transaction_is_a_failure() -> None:
    errored = {**DOCUMENTED_TRANSACTION, "success": False, "error_occured": True}
    body, signature = _signed(errored)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.status is PaymentStatus.FAILED
    assert not event.succeeded


def test_a_refunded_transaction_is_reported_as_refunded() -> None:
    """Distinct from a payment that never succeeded: the money moved twice."""
    refunded = {**DOCUMENTED_TRANSACTION, "is_refunded": True}
    body, signature = _signed(refunded)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.status is PaymentStatus.REFUNDED


def test_our_reference_is_read_back_out_of_merchant_order_id() -> None:
    """The documented round trip, and the whole callback-to-payment mapping."""
    carried = json.loads(json.dumps(DOCUMENTED_TRANSACTION))
    carried["order"]["merchant_order_id"] = "0a4f1e2c-0000-4000-8000-000000000000"
    body, signature = _signed(carried)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.reference == "0a4f1e2c-0000-4000-8000-000000000000"


def test_a_callback_without_our_reference_maps_to_nothing() -> None:
    """Not an error here - the caller decides. A payment that cannot be
    identified is refused upstream rather than guessed at."""
    body, signature = _signed(DOCUMENTED_TRANSACTION)

    event = _provider().verify_callback(payload=body, signature=signature)

    assert event.reference is None
