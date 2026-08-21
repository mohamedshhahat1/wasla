"""Webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac

from app.integrations.whatsapp.signature import compute_signature, verify_signature

APP_SECRET = "meta-app-secret"
BODY = b'{"object":"whatsapp_business_account"}'


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_signature_produced_by_the_app_secret_verifies():
    header = _signature(APP_SECRET, BODY)

    assert verify_signature(payload=BODY, header=header, app_secret=APP_SECRET)


def test_the_computed_header_matches_the_documented_format():
    computed = compute_signature(payload=BODY, app_secret=APP_SECRET)

    assert computed == _signature(APP_SECRET, BODY)
    assert computed.startswith("sha256=")


def test_another_secret_does_not_verify():
    header = _signature("not-the-secret", BODY)

    assert not verify_signature(payload=BODY, header=header, app_secret=APP_SECRET)


def test_a_changed_body_does_not_verify():
    header = _signature(APP_SECRET, BODY)

    assert not verify_signature(payload=BODY + b" ", header=header, app_secret=APP_SECRET)


def test_a_missing_header_is_a_failure_not_a_pass():
    assert not verify_signature(payload=BODY, header=None, app_secret=APP_SECRET)
    assert not verify_signature(payload=BODY, header="", app_secret=APP_SECRET)


def test_an_unconfigured_secret_never_verifies():
    # Otherwise a deployment missing its secret would accept every caller.
    header = _signature(APP_SECRET, BODY)

    assert not verify_signature(payload=BODY, header=header, app_secret="")


def test_a_header_without_the_prefix_does_not_verify():
    digest = hmac.new(APP_SECRET.encode(), BODY, hashlib.sha256).hexdigest()

    assert not verify_signature(payload=BODY, header=digest, app_secret=APP_SECRET)


def test_surrounding_whitespace_is_tolerated():
    header = f"  {_signature(APP_SECRET, BODY)}  "

    assert verify_signature(payload=BODY, header=header, app_secret=APP_SECRET)
