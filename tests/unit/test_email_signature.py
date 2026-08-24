"""Resend webhook signature verification (Svix scheme)."""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.integrations.email.signature import (
    DEFAULT_TOLERANCE_SECONDS,
    compute_signature,
    verify_signature,
)

SECRET = "whsec_" + base64.b64encode(b"svix-signing-key-for-tests").decode()
MESSAGE_ID = "msg_2abcDEF"
TIMESTAMP = "1700000000"
# The moment the timestamp above is current, so expiry is driven rather than
# waited for.
NOW = 1700000000.0
BODY = b'{"type":"email.delivered","data":{"email_id":"re_123"}}'


def _sign(
    *,
    body: bytes = BODY,
    message_id: str = MESSAGE_ID,
    timestamp: str = TIMESTAMP,
    secret: str = SECRET,
) -> str:
    """A header built the way Svix documents it, independently of our code."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = b".".join((message_id.encode(), timestamp.encode(), body))
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _verify(**overrides) -> bool:
    kwargs = {
        "payload": BODY,
        "message_id": MESSAGE_ID,
        "timestamp": TIMESTAMP,
        "signature_header": _sign(),
        "secret": SECRET,
        "now": NOW,
    }
    kwargs.update(overrides)
    return verify_signature(**kwargs)


def test_a_delivery_signed_with_the_secret_verifies():
    assert _verify()


def test_the_computed_signature_matches_the_documented_format():
    computed = compute_signature(
        payload=BODY,
        message_id=MESSAGE_ID,
        timestamp=TIMESTAMP,
        secret=SECRET,
    )

    assert computed is not None
    assert _sign() == f"v1,{computed}"


def test_the_signed_content_is_id_timestamp_and_body_not_the_body_alone():
    # The whole point of the scheme. A signature over just the body would be
    # valid for any delivery carrying that body, which is what makes a captured
    # request replayable under a new id and timestamp.
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    body_only = base64.b64encode(hmac.new(key, BODY, hashlib.sha256).digest()).decode()

    assert not _verify(signature_header=f"v1,{body_only}")


def test_the_hmac_key_is_the_decoded_secret_not_the_prefixed_string():
    # Signing with the literal `whsec_...` text is the classic implementation
    # slip, and it fails closed rather than open - which is why it gets
    # "fixed" by disabling verification instead of by decoding the secret.
    signed = b".".join((MESSAGE_ID.encode(), TIMESTAMP.encode(), BODY))
    wrong = hmac.new(SECRET.encode(), signed, hashlib.sha256).digest()

    assert not _verify(signature_header="v1," + base64.b64encode(wrong).decode())


def test_another_secret_does_not_verify():
    other = "whsec_" + base64.b64encode(b"a-different-signing-key").decode()

    assert not _verify(signature_header=_sign(secret=other))


def test_a_changed_body_does_not_verify():
    assert not _verify(payload=BODY + b" ")


def test_a_signature_cannot_be_replayed_under_a_different_message_id():
    # The header is genuine, for a delivery this is not.
    assert not _verify(message_id="msg_someone_elses")


def test_a_signature_cannot_be_replayed_under_a_different_timestamp():
    fresh = str(int(NOW) + 10)

    assert not _verify(timestamp=fresh)


def test_a_stale_delivery_does_not_verify():
    # A valid signature stays valid forever, so the timestamp window is the
    # only thing bounding how long a captured request can be replayed.
    assert not _verify(now=NOW + DEFAULT_TOLERANCE_SECONDS + 1)


def test_a_delivery_from_the_future_does_not_verify():
    # A forged timestamp would otherwise buy a much longer replay window.
    assert not _verify(now=NOW - DEFAULT_TOLERANCE_SECONDS - 1)


def test_a_delivery_inside_the_window_still_verifies():
    assert _verify(now=NOW + DEFAULT_TOLERANCE_SECONDS)
    assert _verify(now=NOW - DEFAULT_TOLERANCE_SECONDS)


def test_an_unparseable_timestamp_does_not_verify():
    stamp = "not-a-timestamp"
    assert not _verify(timestamp=stamp, signature_header=_sign(timestamp=stamp))


def test_missing_headers_are_a_failure_not_a_pass():
    assert not _verify(message_id=None)
    assert not _verify(timestamp=None)
    assert not _verify(signature_header=None)
    assert not _verify(signature_header="")


def test_an_unconfigured_secret_never_verifies():
    # Otherwise a deployment missing its secret would accept every caller.
    assert not _verify(secret="")
    assert not _verify(secret="   ")


def test_a_malformed_secret_never_verifies():
    # Not valid base64. It must refuse rather than raise: this runs on an
    # unauthenticated request, where an exception is a 500 with a traceback.
    assert not _verify(secret="whsec_not!valid!base64!")
    assert (
        compute_signature(
            payload=BODY,
            message_id=MESSAGE_ID,
            timestamp=TIMESTAMP,
            secret="whsec_not!valid!base64!",
        )
        is None
    )


def test_one_valid_entry_among_several_verifies():
    # How a signing secret is rotated without dropping deliveries: the provider
    # sends a signature per active secret and any one of them may be ours.
    other = "whsec_" + base64.b64encode(b"the-secret-being-rotated-out").decode()
    header = f"{_sign(secret=other)} {_sign()}"

    assert _verify(signature_header=header)


def test_an_unknown_signature_version_is_skipped_not_trusted():
    valid = _sign().removeprefix("v1,")

    assert not _verify(signature_header=f"v9,{valid}")
    assert not _verify(signature_header=valid)


def test_a_garbled_header_does_not_verify():
    assert not _verify(signature_header="v1,")
    assert not _verify(signature_header=",")
    assert not _verify(signature_header="v1")
