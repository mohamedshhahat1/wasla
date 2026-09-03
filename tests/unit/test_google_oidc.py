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
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.db.models.identity import MAX_PROVIDER_SUBJECT_LENGTH
from app.db.models.user import MAX_AVATAR_URL_LENGTH, MAX_EMAIL_LENGTH, MAX_FULL_NAME_LENGTH
from app.integrations.google.oidc import (
    GOOGLE_ISSUERS,
    JWKS_FRESH_SECONDS,
    JWKS_MIN_REFRESH_SECONDS,
    JWKS_STALE_SECONDS,
    MAX_EMAIL_CLAIM_LENGTH,
    MAX_JWKS_BYTES,
    MAX_NAME_LENGTH,
    MAX_PICTURE_URL_LENGTH,
    MAX_SUBJECT_LENGTH,
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
PICTURE = "https://lh3.googleusercontent.com/a/abc123=s96-c"
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


def _claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "name": "A Person",
        "picture": PICTURE,
        "nonce": NONCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _ABSENT}


class _Absent:
    """Marker so a test can remove a claim rather than blank it."""


_ABSENT = _Absent()


def _sign(
    payload: dict[str, Any],
    *,
    key: rsa.RSAPrivateKey | str = _SIGNING_KEY,
    kid: str = KID,
    algorithm: str = "RS256",
) -> str:
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


def _unsigned(payload: dict[str, Any]) -> str:
    """An `alg=none` token, assembled by hand.

    Built from base64 rather than through PyJWT on purpose: whether a given
    PyJWT version is willing to *produce* an unsigned token is not the thing
    under test. Whether this code refuses one is.
    """

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none', 'kid': KID})}.{part(payload)}."


class _FixedKeyRing(GoogleKeyRing):
    """A key ring whose fetches are scripted instead of networked.

    Each entry is either a JWKS document or an exception to raise. The last
    entry repeats, so a test can say "succeed once, then always fail".
    """

    def __init__(self, *responses: Any) -> None:
        super().__init__()
        self.responses = list(responses)
        self.fetches = 0

    async def _fetch(self) -> dict[str, Any]:
        self.fetches += 1
        item = self.responses[min(self.fetches - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        document: dict[str, Any] = item
        return document


def _verifier(*responses: Any) -> tuple[GoogleIdTokenVerifier, _FixedKeyRing]:
    ring = _FixedKeyRing(*(responses or (_jwks((KID, _SIGNING_KEY)),)))
    return GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring), ring


async def _reject(
    verifier: GoogleIdTokenVerifier,
    token: str,
    *,
    nonce: str = NONCE,
) -> str:
    with pytest.raises(GoogleTokenInvalidError) as caught:
        await verifier.verify(id_token=token, nonce=nonce)
    return caught.value.reason


# --- the happy path, so the rejections below mean something -------------------


async def test_a_genuine_token_is_accepted() -> None:
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.subject == SUBJECT
    assert claims.email == EMAIL
    assert claims.email_verified is True
    assert claims.full_name == "A Person"


async def test_the_second_google_issuer_spelling_is_accepted() -> None:
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


async def test_a_token_signed_by_another_key_is_refused() -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(), key=_ATTACKER_KEY)) == "bad_signature"


async def test_a_forgery_with_its_own_key_document_is_refused() -> None:
    """The attack in its real shape.

    Somebody who can serve a key document signs a token with their own key and
    publishes that key under the same `kid`. It must fail on the signature, not
    merely on a key id lookup - and it does, because the document this verifier
    reads comes from a URL that is a module constant.
    """
    verifier, _ = _verifier(_jwks((KID, _SIGNING_KEY)))
    forged = _sign(_claims(), key=_ATTACKER_KEY, kid=KID)
    assert await _reject(verifier, forged) == "bad_signature"


async def test_an_unsigned_token_is_refused_without_fetching_keys() -> None:
    verifier, ring = _verifier()
    assert await _reject(verifier, _unsigned(_claims())) == "unexpected_algorithm"
    # The algorithm is checked before a key is looked up, so `alg=none` cannot
    # be used to make this process fetch anything.
    assert ring.fetches == 0


async def test_an_hs256_token_pretending_to_be_google_is_refused() -> None:
    verifier, ring = _verifier()
    confused = jwt.encode(_claims(), "a-shared-secret", algorithm="HS256", headers={"kid": KID})
    assert await _reject(verifier, confused) == "unexpected_algorithm"
    assert ring.fetches == 0


async def test_a_malformed_token_is_refused() -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, "not-a-jwt") == "malformed"


async def test_a_token_without_a_key_id_is_refused() -> None:
    verifier, _ = _verifier()
    token = jwt.encode(_claims(), _SIGNING_KEY, algorithm="RS256")
    assert await _reject(verifier, token) == "missing_key_id"


# --- claims -------------------------------------------------------------------


async def test_a_foreign_issuer_is_refused() -> None:
    verifier, _ = _verifier()
    token = _sign(_claims(iss="https://accounts.google.com.evil.example"))
    assert await _reject(verifier, token) == "wrong_issuer"


async def test_a_token_for_another_audience_is_refused() -> None:
    verifier, _ = _verifier()
    other = "9999-other.apps.googleusercontent.com"
    assert await _reject(verifier, _sign(_claims(aud=other))) == "wrong_audience"


async def test_an_expired_token_is_refused() -> None:
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
async def test_a_required_claim_cannot_be_missing(claim: str) -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(**{claim: _ABSENT}))) == "missing_claim"


async def test_an_empty_subject_is_refused() -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(sub="   "))) == "missing_subject"


async def test_a_token_without_an_email_is_refused() -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(email=_ABSENT))) == "missing_email"


async def test_a_missing_nonce_is_refused() -> None:
    verifier, _ = _verifier()
    assert await _reject(verifier, _sign(_claims(nonce=_ABSENT))) == "missing_nonce"


async def test_a_nonce_from_another_flow_is_refused() -> None:
    """Replay, in the only form that matters.

    A token genuinely issued by Google for this client, presented to a flow that
    asked for a different nonce. Everything about it verifies except the binding
    to *this* authorization attempt, and that is what must refuse it.
    """
    verifier, _ = _verifier()
    token = _sign(_claims(nonce="a-nonce-from-a-different-flow"))
    assert await _reject(verifier, token, nonce=NONCE) == "wrong_nonce"


async def test_a_replayed_token_fails_against_a_fresh_flow() -> None:
    verifier, _ = _verifier()
    token = _sign(_claims())
    assert (await verifier.verify(id_token=token, nonce=NONCE)).subject == SUBJECT
    # The same token again, in a new flow with a new nonce. The flow store makes
    # the state single-use; the nonce is what makes the *token* single-flow.
    assert await _reject(verifier, token, nonce="the-next-flows-nonce") == "wrong_nonce"


async def test_a_string_email_verified_does_not_count_as_verified() -> None:
    """`"false"` is truthy in Python, which is the whole reason for `is True`."""
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(email_verified="false")), nonce=NONCE)
    assert claims.email_verified is False


async def test_an_unverified_address_is_reported_as_unverified() -> None:
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(email_verified=False)), nonce=NONCE)
    assert claims.email_verified is False


# --- the key ring -------------------------------------------------------------


async def test_an_unknown_key_id_triggers_one_refresh_then_refuses() -> None:
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


async def test_an_unknown_key_id_does_not_refetch_within_the_refresh_window() -> None:
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


async def test_a_rotated_key_is_picked_up_once_the_refresh_window_passes() -> None:
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


async def test_a_known_key_is_served_from_cache() -> None:
    verifier, ring = _verifier(_jwks((KID, _SIGNING_KEY)))
    for _ in range(3):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert ring.fetches == 1


async def test_keys_are_refetched_once_they_are_no_longer_fresh() -> None:
    ring = _FixedKeyRing(_jwks((KID, _SIGNING_KEY)))
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=ring)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)

    ring._fetched_at = time.monotonic() - (JWKS_FRESH_SECONDS + 1)
    ring._last_attempt_at = time.monotonic() - (JWKS_MIN_REFRESH_SECONDS + 1)
    await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert ring.fetches == 2


async def test_a_usable_cache_survives_google_being_unreachable() -> None:
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


async def test_a_cache_past_the_stale_window_is_not_used() -> None:
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


async def test_nothing_can_be_verified_when_the_keys_were_never_fetched() -> None:
    verifier, _ = _verifier(GoogleKeysUnavailableError("down"))
    with pytest.raises(GoogleKeysUnavailableError):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)


async def test_refresh_attempts_are_bounded() -> None:
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
async def test_a_malformed_key_document_verifies_nothing(
    document: dict[str, Any],
) -> None:
    verifier, _ = _verifier(document)
    with pytest.raises(GoogleKeysUnavailableError):
        await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)


# --- the transport ------------------------------------------------------------


class _FakeStream:
    def __init__(
        self,
        *,
        chunks: Sequence[bytes] = (),
        error: Exception | None = None,
        http_error: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._error = error
        self._http_error = http_error

    async def __aenter__(self) -> _FakeStream:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self._http_error:
            raise httpx.HTTPError("refused")

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _FakeHttpClient:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    async def __aenter__(self) -> _FakeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def stream(self, _method: str, _url: str) -> _FakeStream:
        return self._stream


def _install(monkeypatch: pytest.MonkeyPatch, stream: _FakeStream) -> None:
    monkeypatch.setattr(
        "app.integrations.google.oidc.build_guarded_client",
        lambda *, timeout: _FakeHttpClient(stream),
    )


async def test_the_real_fetch_reads_a_key_document(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.dumps(_jwks((KID, _SIGNING_KEY))).encode()
    _install(monkeypatch, _FakeStream(chunks=[document]))
    verifier = GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=GoogleKeyRing())
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.subject == SUBJECT


async def test_a_timeout_fetching_keys_is_not_a_rejected_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeStream(error=httpx.TimeoutException("slow")))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_an_error_response_fetching_keys_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeStream(http_error=True))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_an_oversized_key_document_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read is bounded, not merely checked after the fact."""
    _install(monkeypatch, _FakeStream(chunks=[b"x" * (MAX_JWKS_BYTES + 1)]))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_a_key_document_that_is_not_json_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeStream(chunks=[b"<html>an error page</html>"]))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


async def test_a_key_document_that_is_not_an_object_is_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeStream(chunks=[b'["not", "an", "object"]']))
    with pytest.raises(GoogleKeysUnavailableError):
        await GoogleKeyRing().key_for(KID)


# --- The `picture` claim -------------------------------------------------
#
# The only claim whose value is handed to a browser, and therefore the only one
# with a shape rule rather than just a type check. Every case below is about
# what may reach `users.avatar_url`, because nothing downstream re-checks it.


async def test_a_picture_is_carried_through() -> None:
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims()), nonce=NONCE)
    assert claims.picture == PICTURE


@pytest.mark.parametrize(
    "value",
    [
        _ABSENT,
        None,
        "",
        "   ",
        42,
        ["https://example.com/a.png"],
        {"url": "https://example.com/a.png"},
    ],
)
async def test_a_missing_or_untyped_picture_becomes_none(value: object) -> None:
    """Absent and unusable are the same answer: a person with no picture."""
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(picture=value)), nonce=NONCE)
    assert claims.picture is None


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "http://lh3.googleusercontent.com/a/abc",
        "//evil.example.com/a.png",
        "https:///nohost",
        "not a url at all",
    ],
)
async def test_a_hostile_picture_url_is_refused_rather_than_stored(hostile: str) -> None:
    """The case this validation exists for.

    Google signs what the account says; an ID token is not a promise that every
    claim inside it is safe to render. A `javascript:` or `data:` value is a
    stored-XSS payload arriving under a perfectly valid signature, and plain
    `http` is a mixed-content warning at best. None of them may reach the
    column, and none of them may fail the login either - the person simply has
    no picture.
    """
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(picture=hostile)), nonce=NONCE)
    assert claims.picture is None
    assert claims.subject == SUBJECT


async def test_an_overlong_picture_url_is_refused() -> None:
    """Longer than the column, so refusing here is what stops a write error."""
    long_url = "https://lh3.googleusercontent.com/" + ("a" * MAX_PICTURE_URL_LENGTH)
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(picture=long_url)), nonce=NONCE)
    assert claims.picture is None


async def test_a_picture_at_the_length_limit_is_kept() -> None:
    """The boundary, so the cap cannot quietly become off-by-one."""
    prefix = "https://lh3.googleusercontent.com/"
    exact = prefix + "a" * (MAX_PICTURE_URL_LENGTH - len(prefix))
    assert len(exact) == MAX_PICTURE_URL_LENGTH
    verifier, _ = _verifier()
    claims = await verifier.verify(id_token=_sign(_claims(picture=exact)), nonce=NONCE)
    assert claims.picture == exact


# --- Claim bounds: what will not fit, and what happens to it (SEC-11) --------
#
# Google's own validation constrains all of these, so none is reachable by an
# ordinary account. That is a statement about Google's input validation, not
# about ours: an ID token is a signed assertion of whatever the account says,
# and `picture` in the section above is the same argument already accepted once.
#
# The two answers are deliberately different, and the difference is the point.
# An identifier that will not fit is refused, because shortening one merges two
# identities. Decoration that will not fit is shortened, because refusing it
# would deny a login over a display name.


def test_the_bounds_are_the_columns_and_not_a_second_copy_of_them() -> None:
    """The check and the column must be one number.

    Two numbers that are meant to be equal and are written down separately are
    a latent 500: the day the column changes, the validator keeps passing
    values the column will refuse, and `DataError` is not `IntegrityError`, so
    nothing catches it. This is the assertion that makes the import load-bearing
    rather than decorative.
    """
    assert MAX_SUBJECT_LENGTH == MAX_PROVIDER_SUBJECT_LENGTH
    assert MAX_EMAIL_CLAIM_LENGTH == MAX_EMAIL_LENGTH
    assert MAX_NAME_LENGTH == MAX_FULL_NAME_LENGTH
    assert MAX_PICTURE_URL_LENGTH == MAX_AVATAR_URL_LENGTH


async def test_an_oversized_subject_is_refused_rather_than_shortened() -> None:
    """The one claim where truncation would be an authentication bypass.

    The subject is what every login looks an account up by. Two subjects
    agreeing on their first `MAX_SUBJECT_LENGTH` characters, shortened to fit,
    become one stored value and therefore one account - so the token is
    declined instead.
    """
    verifier, _ = _verifier()

    reason = await _reject(verifier, _sign(_claims(sub="9" * (MAX_SUBJECT_LENGTH + 1))))

    assert reason == "subject_too_long"


async def test_a_subject_at_the_limit_is_accepted_whole() -> None:
    """The boundary, and that nothing was trimmed on the way through."""
    exact = "9" * MAX_SUBJECT_LENGTH
    verifier, _ = _verifier()

    claims = await verifier.verify(id_token=_sign(_claims(sub=exact)), nonce=NONCE)

    assert claims.subject == exact


async def test_two_oversized_subjects_cannot_be_shortened_onto_one_identity() -> None:
    """The collision the refusal exists to prevent, written as a test.

    Two distinct Google accounts sharing a long common prefix. Under truncation
    both would store the same `provider_subject`, and the second would sign in
    as the first. Neither is accepted, so there is nothing to collide.
    """
    shared = "9" * MAX_SUBJECT_LENGTH
    verifier, _ = _verifier()

    first = await _reject(verifier, _sign(_claims(sub=shared + "1")))
    second = await _reject(verifier, _sign(_claims(sub=shared + "2")))

    assert first == second == "subject_too_long"


async def test_an_oversized_email_is_refused_rather_than_shortened() -> None:
    """At enrolment the address *is* the account, and the collision check
    compares it - so a shortened one could be matched against somebody else's."""
    verifier, _ = _verifier()
    oversized = "e" * (MAX_EMAIL_CLAIM_LENGTH - len("@example.com") + 1) + "@example.com"
    assert len(oversized) > MAX_EMAIL_CLAIM_LENGTH

    assert await _reject(verifier, _sign(_claims(email=oversized))) == "email_too_long"


async def test_an_email_is_measured_in_the_form_it_will_be_stored_in() -> None:
    """Lower-casing can lengthen a string, and the stored form is lower-cased.

    `str.lower()` maps U+0130 to two characters, so an address inside the bound
    as written is outside it by the time `normalise_email` is done with it.
    Measuring the value as it arrives would let exactly that through to the
    column.
    """
    verifier, _ = _verifier()
    grows = "İ" * (MAX_EMAIL_CLAIM_LENGTH - len("@example.com"))
    address = grows + "@example.com"
    assert len(address) <= MAX_EMAIL_CLAIM_LENGTH
    assert len(address.lower()) > MAX_EMAIL_CLAIM_LENGTH

    assert await _reject(verifier, _sign(_claims(email=address))) == "email_too_long"


async def test_an_oversized_name_is_shortened_and_the_login_still_succeeds() -> None:
    """The opposite call, for the reason `picture` degrades rather than raising.

    Nothing is authorized by a display name, no lookup compares one, and no
    column is unique on one - so shortening cannot merge two people. Refusing
    would let an unusual Google profile name lock somebody out of a product
    they have paid for, which is a real cost paid to prevent nothing.
    """
    verifier, _ = _verifier()

    claims = await verifier.verify(
        id_token=_sign(_claims(name="N" * (MAX_NAME_LENGTH + 300))),
        nonce=NONCE,
    )

    assert len(claims.full_name or "") == MAX_NAME_LENGTH
    # And the identity is untouched by any of it.
    assert claims.subject == SUBJECT
    assert claims.email == EMAIL


async def test_a_name_at_the_limit_is_kept_whole() -> None:
    """The boundary, so the cap cannot quietly become off-by-one."""
    exact = "N" * MAX_NAME_LENGTH
    verifier, _ = _verifier()

    claims = await verifier.verify(id_token=_sign(_claims(name=exact)), nonce=NONCE)

    assert claims.full_name == exact


async def test_a_name_is_stripped_before_it_is_measured() -> None:
    """Whitespace is not content, and counting it would shorten a name that fits."""
    verifier, _ = _verifier()
    padded = "   " + "N" * MAX_NAME_LENGTH + "   "

    claims = await verifier.verify(id_token=_sign(_claims(name=padded)), nonce=NONCE)

    assert claims.full_name == "N" * MAX_NAME_LENGTH


async def test_a_refusal_over_a_length_carries_no_token_material() -> None:
    """`reason` is a category. It must not become a way to read the claim back.

    The reason travels into logs and the audit trail, and a value echoed there
    is a second copy of provider data in a place people read.
    """
    verifier, _ = _verifier()
    subject = "9" * (MAX_SUBJECT_LENGTH + 1)

    with pytest.raises(GoogleTokenInvalidError) as caught:
        await verifier.verify(id_token=_sign(_claims(sub=subject)), nonce=NONCE)

    rendered = f"{caught.value.reason} {caught.value}"
    assert subject not in rendered
    assert "eyJ" not in rendered
