"""The saved-card callback signature, pinned to the vendor's worked example.

A second signature scheme, and it exists because Paymob signs card-token
notifications over a *different* set of fields from transaction ones - eight
rather than twenty, in their own order. That matters more than it sounds: a
token callback checked against the transaction field list would produce a
digest over the wrong string, which is not a weaker check but no check at all.

Anchored externally for the same reason the transaction one is. The
documentation publishes a sample token object and the exact concatenated string
it produces
(developers.paymob.com/paymob-docs/developers/webhook-callbacks-and-hmac/hmac/hmac-for-card-tokens,
last updated 24 August 2026, read 29 August 2026), and `EXPECTED_MESSAGE` below
is that string copied verbatim.

Unlike the transaction page, this one also publishes the resulting **digest**
for a known secret - but not the secret, so it still cannot be reproduced. The
message is what is ours to get wrong, and it is what is pinned.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.billing.checkout import CallbackVerificationError
from app.integrations.billing.paymob import (
    TOKEN_HMAC_FIELDS,
    PaymobProvider,
    token_hmac_message,
    token_hmac_signature,
)

# The card token object from the documentation's worked example.
DOCUMENTED_TOKEN = {
    "id": 15978654,
    "token": "3f22ce8a4e77125c70f0bc69830e34c36df469351e2fa6be76428be4",
    "masked_pan": "xxxx-xxxx-xxxx-2346",
    "merchant_id": 1053928,
    "card_subtype": "MasterCard",
    "created_at": "2026-08-24T13:28:31.015314",
    "email": "kiyedi3052@claspira.com",
    "order_id": 593881581,
}

# Copied verbatim from the documentation's "HMAC Concatenated String" panel.
EXPECTED_MESSAGE = (
    "MasterCard2026-08-24T13:28:31.015314kiyedi3052@claspira.com15978654"
    "xxxx-xxxx-xxxx-234610539285938815813f22ce8a4e77125c70f0bc69830e34c36df469351e2fa6be76428be4"
)

SECRET = "a-test-hmac-secret"


def _provider() -> PaymobProvider:
    return PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret=SECRET,
        integration_ids=[4097558],
    )


def _signed(token: dict, *, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps({"type": "TOKEN", "obj": token}).encode("utf-8")
    return body, token_hmac_signature(token, secret=secret)


def test_the_signed_string_matches_the_documented_example() -> None:
    """The whole point of this file.

    A reordered field or a dropped one fails here rather than against a live
    merchant account, where the symptom would be every saved card being
    rejected and the tempting fix being to stop verifying.
    """
    assert token_hmac_message(DOCUMENTED_TOKEN) == EXPECTED_MESSAGE


def test_eight_fields_are_signed_in_the_documented_order() -> None:
    """The count is part of the contract, and it is not the transaction's."""
    assert TOKEN_HMAC_FIELDS == (
        "card_subtype",
        "created_at",
        "email",
        "id",
        "masked_pan",
        "merchant_id",
        "order_id",
        "token",
    )


def test_a_correctly_signed_token_is_accepted() -> None:
    body, signature = _signed(DOCUMENTED_TOKEN)

    saved = _provider().verify_token_callback(payload=body, signature=signature)

    assert saved.token == DOCUMENTED_TOKEN["token"]
    assert saved.masked_pan == "xxxx-xxxx-xxxx-2346"
    assert saved.brand == "MasterCard"
    assert saved.order_reference == "593881581"


TAMPERINGS = [
    ("card_subtype", "Visa"),
    ("created_at", "2020-01-01T00:00:00.000000"),
    ("email", "somebody@else.example"),
    ("id", 1),
    ("masked_pan", "xxxx-xxxx-xxxx-0000"),
    ("merchant_id", 1),
    ("order_id", 1),
    ("token", "a-different-token"),
]


@pytest.mark.parametrize(("field", "value"), TAMPERINGS, ids=[item[0] for item in TAMPERINGS])
def test_changing_any_signed_field_invalidates_the_token(field: str, value: object) -> None:
    """Parameterised over the documented list rather than spot-checked.

    `token` is the one that matters most - it is what charges the card - but
    `order_id` is the one that would let a genuine signed notification be
    replayed against a different workspace's checkout.
    """
    body, signature = _signed(DOCUMENTED_TOKEN)
    tampered = json.loads(body)
    assert tampered["obj"][field] != value
    tampered["obj"][field] = value

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(
            payload=json.dumps(tampered).encode("utf-8"),
            signature=signature,
        )


def test_every_documented_field_is_covered_by_the_tampering_matrix() -> None:
    assert {field for field, _ in TAMPERINGS} == set(TOKEN_HMAC_FIELDS)


def test_a_transaction_signature_does_not_verify_a_token_callback() -> None:
    """The two schemes must not be interchangeable.

    Signing the same object with the transaction field list produces a digest
    over a different string. Accepting it would mean the eight documented
    fields were never actually checked - the failure mode that looks like
    working code.
    """
    from app.integrations.billing.paymob import hmac_signature

    body = json.dumps({"type": "TOKEN", "obj": DOCUMENTED_TOKEN}).encode("utf-8")
    wrong_scheme = hmac_signature(DOCUMENTED_TOKEN, secret=SECRET)

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=body, signature=wrong_scheme)


def test_a_transaction_callback_is_not_read_as_a_saved_card() -> None:
    """`type` is checked, so the two paths cannot be crossed by mistake."""
    body = json.dumps({"type": "TRANSACTION", "obj": DOCUMENTED_TOKEN}).encode("utf-8")
    signature = token_hmac_signature(DOCUMENTED_TOKEN, secret=SECRET)

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=body, signature=signature)


def test_a_saved_card_callback_is_not_read_as_a_transaction() -> None:
    """And the other way round, which is the direction that would settle money."""
    body = json.dumps({"type": "TOKEN", "obj": DOCUMENTED_TOKEN}).encode("utf-8")

    with pytest.raises(CallbackVerificationError):
        _provider().verify_callback(payload=body, signature="0" * 128)


@pytest.mark.parametrize("signature", [None, "", "not-hex", "0" * 128])
def test_a_missing_or_wrong_signature_is_refused(signature: str | None) -> None:
    body, _ = _signed(DOCUMENTED_TOKEN)

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=body, signature=signature)


def test_a_signature_from_a_different_secret_is_refused() -> None:
    body, signature = _signed(DOCUMENTED_TOKEN, secret="somebody-elses-secret")

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=body, signature=signature)


@pytest.mark.parametrize(
    "payload",
    [b"", b"not json", b"[]", b"{}", b'{"type": "TOKEN"}', b'{"type": "TOKEN", "obj": null}'],
)
def test_a_malformed_body_is_refused_rather_than_parsed_around(payload: bytes) -> None:
    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=payload, signature="0" * 128)


def test_a_verified_callback_without_a_token_is_refused() -> None:
    """A correctly signed notification carrying nothing chargeable.

    Refused rather than stored, because a payment method row with no token is
    a card that can never be charged and would sit in somebody's account
    looking usable.
    """
    empty = {**DOCUMENTED_TOKEN, "token": ""}
    body, signature = _signed(empty)

    with pytest.raises(CallbackVerificationError):
        _provider().verify_token_callback(payload=body, signature=signature)


def test_no_card_number_is_read_out_of_the_callback() -> None:
    """Only what the provider already masks reaches the domain object.

    `SavedPaymentMethod` has no field for a PAN, and this pins that a payload
    carrying one anyway does not smuggle it through.
    """
    from dataclasses import fields

    from app.integrations.billing.checkout import SavedPaymentMethod

    names = {f.name for f in fields(SavedPaymentMethod)}
    assert not (names & {"pan", "card_number", "cvv", "expiry"})

    body, signature = _signed(DOCUMENTED_TOKEN)
    saved = _provider().verify_token_callback(payload=body, signature=signature)
    assert saved.masked_pan is not None
    assert "xxxx" in saved.masked_pan
