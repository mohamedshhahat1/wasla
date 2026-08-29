"""Adversarial tests for Google ID token validation.

Real RSA keys, real signatures. Nothing here stubs the verifier: a test that
patches `jwt.decode` proves the code calls a function, and proves nothing at all
about whether a forgery is refused. Every rejection below is a rejection the
real cryptography performed.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations.google.oidc import (
    GOOGLE_ISSUERS,
    JWKS_FRESH_SECONDS,
    JWKS_MIN_REFRESH_SECONDS,
    JWKS_STALE_SECONDS,
    MAX_JWKS_BYTES,
    GoogleIdTokenVerifier,
    GoogleKeyRing,
    GoogleKeysUnavailableError,
    GoogleTokenInvalidError,
)

CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"
KID = "test-key-1"
ROTATED_KID = "test-key-2"
SUBJECT = "109876543210987654321"
EMAIL = "person@example.com"
NONCE = "flow-nonce-value"
ISSUER = "https://accounts.google.com"

# Generated once per module. Two of them, because "signed by a key that is not
# the published one" is the single most important case in this file and it needs
# a second real key to be a real test.
_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# A third, kept separate from the attacker's on purpose. A rotated key is one
# Google legitimately published; reusing `_ATTACKER_KEY` for it would make the
# rotation tests read as though accepting a forgery were the desired outcome.
_ROTATED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(*keys: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    """A JWKS document for the given (kid, private key) pairs."""
    entries = []
    for kid, private in keys:
        jwk: dict[str, Any] = dict(
            jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        )
        jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
        entries.append(jwk)
    return {"keys": entries}


def _claims(**overrides):
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "name": "A Person",
        "nonce": NONCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _ABSENT}


class _Absent:
    """Marker so a test can remove a claim rather than blank it."""


_ABSENT = _Absent()


def _sign(payload, *, key=_SIGNING_KEY, kid=KID, algorithm="RS256"):
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


def _unsigned(payload):
    """An `alg=none` token, assembled by hand.

    Built from base64 rather than through PyJWT on purpose: whether a given
    PyJWT version is willing to *produce* an unsigned token is not the thing
    under test. Whether this code refuses one is.
    """

    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none', 'kid': KID})}.{part(payload)}."


class _FixedKeyRing(GoogleKeyRing):
    """A key ring whose fetches are scripted instead of networked.

    Each entry is either a JWKS document or an exception to raise. The last
    entry repeats, so a test can say "succeed once, then always fail".
    """

    def __init__(self, *responses: Any):
        super().__init__()
        self.responses = list(responses)
        self.fetches = 0

    async def _fetch(self):
        self.fetches += 1
        item = self.responses[min(self.fetches - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _verifier(*responses: Any):
    ring = _FixedKeyRing(*(responses or (_jwks((KID, _SIGNING_KEY)),)))
    return GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring), ring


async def _reject(verifier, token, *, nonce=NONCE):
    with pytest.raises(GoogleTokenInvalidError) as caught:
        await verifier.verify(id_token=token, nonce=nonce)
    return caught.value.reason


# --- the happy path, so the rejections below mean something -------------------


async def test_a_genuine_token_is_accepted():
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.subject == SUBJECT
    assert claims.email == EMAIL
    assert claims.email_verified is True
    assert claims.full_name == "A Person"


async def test_the_second_google_issuer_spelling_is_accepted():
    # Google issues both. A verifier that delegated `iss` to PyJWT's single
    # `issuer=` parameter would refuse half of Google's real tokens.
    assert "accounts.google.com" in GOOGLE_ISSUERS
    verifier, _ = _verifier()
    claims = await verifier.verify(
        id_token=_sign(_claims(iss="accounts.google.com")),
        nonce=NONCE,
    )
    assert claims.subject == SUBJECT


# --- cryptography -------------------------------------------------------------


async def test_a_token_signed_by_another_key_is_refused():
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(), key=_ATTACKER_KEY)) == "bad_signature"


async def test_a_forgery_with_its_own_key_document_is_refused():
    """The attack in its real shape.

    Somebody who can serve a key document signs a token with their own key and
    publishes that key under the same `kid`. It must fail on the signature, not
    merely on a key id lookup - and it does, because the document this verifier
    reads comes from a URL that is a module constant.
    """
    verifier, _ = _verifier(_jwks((KID, _SIGNING_KEY)))
    forged = _sign(_claims(), key=_ATTACKER_KEY, kid=KID)
    assert await _reject(verifier, forged) == "bad_signature"


async def test_an_unsigned_token_is_refused_without_fetching_keys():
    verifier, ring = _verifier()
    assert await _reject(verifier, _unsigned(_claims())) == "unexpected_algorithm"
    # The algorithm is checked before a key is looked up, so `alg=none` cannot
    # be used to make this process fetch anything.
    assert ring.fetches == 0


async def test_an_hs256_token_pretending_to_be_google_is_refused():
    verifier, ring = _verifier()
    confused = jwt.encode(_claims(), "a-shared-secret", algorithm="HS256", headers={"kid": KID})
    assert await _reject(verifier, confused) == "unexpected_algorithm"
    assert ring.fetches == 0


async def test_a_malformed_token_is_refused():
    verifier, _ = _verifier()
    assert await _reject(verifier, "not-a-jwt") == "malformed"


async def test_a_token_without_a_key_id_is_refused():
    verifier, _ = _verifier()
    token = jwt.encode(_claims(), _SIGNING_KEY, algorithm="RS256")
    assert await _reject(verifier, token) == "missing_key_id"


# --- claims -------------------------------------------------------------------


async def test_a_foreign_issuer_is_refused():
    verifier, _ = _verifier()
    token = _sign(_claims(iss="https://accounts.google.com.evil.example"))
    assert await _reject(verifier, token) == "wrong_issuer"


async def test_a_token_for_another_audience_is_refused():
    verifier, _ = _verifier()
    other = "9999-other.apps.googleusercontent.com"
    assert await _reject(verifier, _sign(_claims(aud=other))) == "wrong_audience"


async def test_an_expired_token_is_refused():
    verifier, _ = _verifier()
    stale = datetime.now(UTC) - timedelta(hours=2)
    token = _sign(
        _claims(
            iat=int(stale.timestamp()),
            exp=int((stale + timedelta(minutes=5)).timestamp()),
        )
    )
    assert await _reject(verifier, token) == "expired"


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "iat", "sub"])
async def test_a_required_claim_cannot_be_missing(claim):
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(**{claim: _ABSENT}))) == "missing_claim"


async def test_an_empty_subject_is_refused():
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(sub="   "))) == "missing_subject"


async def test_a_token_without_an_email_is_refused():
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(email=_ABSENT))) == "missing_email"


async def test_a_missing_nonce_is_refused():
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(nonce=_ABSENT))) == "missing_nonce"


async def test_a_nonce_from_another_flow_is_refused():
    """Replay, in the only form that matters.

    A token genuinely issued by Google for this client, presented to a flow that
    asked for a different nonce. Everything about it verifies except the binding
    to *this* authorization attempt, and that is what must refuse it.
    """
    verifier, _ = _verifier()
    token = _sign(_claims(nonce="a-nonce-from-a-different-flow"))
    assert await _reject(verifier, token, nonce=NONCE) == "wrong_nonce"


async def test_a_replayed_token_fails_against_a_fresh_flow():
    verifier, _ = _verifier()
    token = _sign(_claims())
    assert (await verifier.verify(id_token=token, nonce=NONCE)).subject == SUBJECT
    # The same token again, in a new flow with a new nonce. The flow store makes
    # the state single-use; the nonce is what makes the *token* single-flow.
    assert await _reject(verifier, token, nonce="the-next-flows-nonce") == "wrong_nonce"


async def test_a_string_email_verified_does_not_count_as_verified():
    """`"false"` is truthy in Python, which is the whole reason for `is True`."""
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(email_verified="false")), nonce=NONCE)
    assert claims.email_verified is False


async def test_an_unverified_address_is_reported_as_unverified():
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(email_verified=False)), nonce=NONCE)
    assert claims.email_verified is False


# --- the key ring -------------------------------------------------------------


async def test_an_unknown_key_id_triggers_one_refresh_then_refuses():
    verifier, ring = _verifier(_jwks((KID, _SIGNING_KEY)))
    token = _sign(_claims(), kid="a-key-google-never-published")
    assert await _reject(verifier, token) == "unknown_key_id"
    assert ring.fetches == 1


def _rotating_ring() -> _FixedKeyRing:
    """A ring that publishes a second key on its next fetch."""
    return _FixedKeyRing(
        _jwks((KID, _SIGNING_KEY)),
        _jwks((KID, _SIGNING_KEY), (ROTATED_KID, _ROTATED_KEY)),
    )


async def test_an_unknown_key_id_does_not_refetch_within_the_refresh_window():
    """The documented cost of the refresh bound, asserted rather than assumed.

    `_refresh_allowed` exists to stop a stream of tokens carrying invented key
    ids from making this process hammer Google on an attacker's schedule, and
    it is deliberately blunt: for up to `JWKS_MIN_REFRESH_SECONDS` a genuinely
    rotated key is indistinguishable from an invented one, and both are
    refused. That is the trade the module docstring claims to make, so it is
    worth a test - an implementation that quietly refetched here would still
    pass every other case in this file while being a denial-of-service
    amplifier pointed at our own dependency.
    """
    ring = _rotating_ring()
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    assert (await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)).subject == SUBJECT

    rotated = _sign(_claims(), key=_ROTATED_KEY, kid=ROTATED_KID)
    with pytest.raises(GoogleTokenInvalidError):
        await verifier.verify(id_token=rotated, nonce=NONCE)
    assert ring.fetches == 1


async def test_a_rotated_key_is_picked_up_once_the_refresh_window_passes():
    """Google rotates without notice, and the bound above must not be a wall.

    The other half of the trade: once a fetch is permitted again, a key that
    was unknown a minute ago verifies normally. Without this, the throttle
    would be a permanent refusal rather than a delay.
    """
    ring = _rotating_ring()
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    assert (await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)).subject == SUBJECT

    ring._last_attempt_at = time.monotonic() - (JWKS_MIN_REFRESH_SECONDS + 1)
    rotated = _sign(_claims(), key=_ROTATED_KEY, kid=ROTATED_KID)
    assert (await verifier.verify(id_token=rotated, nonce=NONCE)).subject == SUBJECT
    assert ring.fetches == 2


async def test_a_known_key_is_served_from_cache():
    verifier, ring = _verifier(_jwks((KID, _SIGNING_KEY)))
    for _ in range(3):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert ring.fetches == 1


async def test_keys_are_refetched_once_they_are_no_longer_fresh():
    ring = _FixedKeyRing(_jwks((KID, _SIGNING_KEY)))
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)

    ring._fetched_at = time.monotonic() - (JWKS_FRESH_SECONDS + 1)
    ring._last_attempt_at = time.monotonic() - (JWKS_MIN_REFRESH_SECONDS + 1)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert ring.fetches == 2


async def test_a_usable_cache_survives_google_being_unreachable():
    """The stale policy, first half: a blip must not take sign-in down."""
    ring = _FixedKeyRing(
        _jwks((KID, _SIGNING_KEY)),
        GoogleKeysUnavailableError("down"),
    )
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)

    ring._fetched_at = time.monotonic() - (JWKS_FRESH_SECONDS + 1)
    ring._last_attempt_at = time.monotonic() - (JWKS_MIN_REFRESH_SECONDS + 1)
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.subject == SUBJECT
    assert ring.fetches == 2


async def test_a_cache_past_the_stale_window_is_not_used():
    """The stale policy, second half - the part a TTL-only cache gets wrong.

    A key old enough that Google could have withdrawn it without this process
    hearing about it is not a fallback. Refusing is correct even though the
    cached key would still verify the signature.
    """
    ring = _FixedKeyRing(
        _jwks((KID, _SIGNING_KEY)),
        GoogleKeysUnavailableError("down"),
    )
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)

    ring._fetched_at = time.monotonic() - (JWKS_STALE_SECONDS + 1)
    ring._last_attempt_at = time.monotonic() - (JWKS_MIN_REFRESH_SECONDS + 1)
    with pytest.raises(GoogleKeysUnavailableError):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)


async def test_nothing_can_be_verified_when_the_keys_were_never_fetched():
    verifier, _ = _verifier(GoogleKeysUnavailableError("down"))
    with pytest.raises(GoogleKeysUnavailableError):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)


async def test_refresh_attempts_are_bounded():
    """A stream of forged key ids must not become a load generator."""
    ring = _FixedKeyRing(_jwks((KID, _SIGNING_KEY)))
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    for index in range(5):
        await _reject(verifier, _sign(_claims(), kid=f"invented-{index}"))
    assert ring.fetches == 1


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"keys": []},
        {"keys": [{"kty": "RSA", "kid": KID}]},
        {"keys": [{"kty": "nonsense", "kid": KID, "n": "!!", "e": "!!"}]},
    ],
    ids=["empty", "no-keys", "incomplete-key", "unusable-key"],
)
async def test_a_malformed_key_document_verifies_nothing(document):
    verifier, _ = _verifier(document)
    with pytest.raises(GoogleKeysUnavailableError):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)


# --- the transport ------------------------------------------------------------


class _FakeStream:
    def __init__(self, *, chunks=(), error=None, http_error=False):
        self._chunks = list(chunks)
        self._error = error
        self._http_error = http_error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_: object):
        return False

    def raise_for_status(self):
        if self._http_error:
            raise httpx.HTTPError("refused")

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeHttpClient:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        return False

    def stream(self, _method, _url):
        return self._stream


def _install(monkeypatch, stream):
    monkeypatch.setattr(
        "app.integrations.google.oidc.build_guarded_client",
        lambda *, timeout: _FakeHttpClient(stream),
    )


async def test_the_real_fetch_reads_a_key_document(monkeypatch):
    document = json.dumps(_jwks((KID, _SIGNING_KEY))).encode()
    _install(monkeypatch, _FakeStream(chunks=[document]))
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=GoogleKeyRing())
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.subject == SUBJECT


async def test_a_timeout_fetching_keys_is_not_a_rejected_login(monkeypatch):
    _install(monkeypatch, _FakeStream(error=httpx.TimeoutException("slow")))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_an_error_response_fetching_keys_is_handled(monkeypatch):
    _install(monkeypatch, _FakeStream(http_error=True))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_an_oversized_key_document_is_abandoned(monkeypatch):
    """The read is bounded, not merely checked after the fact."""
    _install(monkeypatch, _FakeStream(chunks=[b"x" * (MAX_JWKS_BYTES + 1)]))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_a_key_document_that_is_not_json_is_handled(monkeypatch):
    _install(monkeypatch, _FakeStream(chunks=[b"<html>an error page</html>"]))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_a_key_document_that_is_not_an_object_is_handled(monkeypatch):
    _install(monkeypatch, _FakeStream(chunks=[b'["not", "an", "object"]']))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)
