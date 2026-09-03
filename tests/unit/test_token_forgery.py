"""Tokens somebody else made, presented as ours.

The existing suite covers tokens this application minted — round trips, expiry,
a wrong signing key, a tampered payload. This file covers the ones an attacker
mints, which is a different question: not "does a good token work" but "does a
*plausible* token fail".

Three families, and each is a real published attack rather than a hypothetical:

**Algorithm confusion.** `alg: none` is the oldest JWT vulnerability there is,
and it is a library-configuration bug rather than a cryptography one - a
verifier that reads the algorithm out of the header the attacker wrote will
happily accept an unsigned token. `tests/unit/test_config.py` already proves
`none` cannot be *configured*; that is a different property from a forged token
being *refused*, and only the second one is the attack.

**Missing claims.** A token omitting any required claim must be refused. These
tests assert that *behaviour*, and deliberately do not claim to pin the
`options={"require": [...]}` list, because they cannot: gutting that list to
`["sub", "exp"]` leaves every test here passing. Each claim is caught a second
time anyway - `sub`, `jti`, `iat` and `exp` by the `KeyError` when the payload
is read, `typ` by the explicit comparison, and `iss` by PyJWT's `issuer=`
argument. That redundancy is worth having and worth naming honestly: the
refusal is held by two layers, and these tests hold the outcome rather than
either mechanism.

**Wrong issuer.** Cross-issuer acceptance matters the day a second service
signs with a shared secret - a token minted for something else should not open
this one.

Nothing here uses the application's own minting functions except as a control,
because a test that can only build valid tokens cannot express any of this.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import ISSUER, TokenType, create_access_token, decode_token

SECRET = "a-signing-secret-long-enough-for-the-validator-to-accept-it"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", jwt_secret=SECRET)


def _claims(**overrides) -> dict:
    """A payload shaped exactly like one this application would mint."""
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": TokenType.ACCESS.audience,
        "sub": str(uuid.uuid4()),
        "typ": TokenType.ACCESS.value,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "ver": 0,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def _decode(token: str, settings: Settings):
    return decode_token(token, settings=settings, expected_type=TokenType.ACCESS)


# ------------------------------------------------------- the control


def test_a_token_this_application_minted_is_accepted(settings: Settings) -> None:
    """The control. Without it every refusal below could be a broken fixture."""
    subject = uuid.uuid4()
    token, _ = create_access_token(settings=settings, subject=subject, token_version=0)

    assert _decode(token, settings).subject == subject


def test_a_hand_built_token_with_the_right_secret_is_accepted(settings: Settings) -> None:
    """The second control, and the more important one.

    It proves the forgeries below are refused for the reason each names, rather
    than because a hand-built token is somehow rejected wholesale.
    """
    token = jwt.encode(_claims(), SECRET, algorithm="HS256")

    assert _decode(token, settings) is not None


# ------------------------------------------------- algorithm confusion


def test_an_unsigned_token_is_refused(settings: Settings) -> None:
    """`alg: none`, the oldest JWT attack there is.

    A verifier that trusts the header's algorithm accepts this with no
    signature at all, and the attacker writes every claim including `sub`.
    """
    token = jwt.encode(_claims(), key="", algorithm="none")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_a_token_with_a_forged_none_header_is_refused(settings: Settings) -> None:
    """The hand-assembled variant, in case the library refuses to mint one.

    Built from base64 segments so nothing in PyJWT's encoder can soften it:
    this is the exact byte sequence an attacker would send.
    """
    import base64
    import json

    def segment(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    token = f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(_claims())}."

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_a_token_signed_with_a_different_hmac_algorithm_is_refused(
    settings: Settings,
    algorithm: str,
) -> None:
    """The deployment pins one algorithm; a token naming another is not it.

    These are all algorithms this application *could* be configured for, which
    is what makes them the interesting case - the secret is the same, so only
    the allowlist stands between them and acceptance.
    """
    token = jwt.encode(_claims(), SECRET, algorithm=algorithm)

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_a_token_signed_with_the_secret_as_an_asymmetric_key_is_refused(
    settings: Settings,
) -> None:
    """Key confusion: the HMAC secret presented as though it were a public key.

    The classic escalation of `alg` confusion. PyJWT refuses to *mint* this, so
    the header is swapped after signing - which is exactly what an attacker
    does.
    """
    import base64
    import json

    signed = jwt.encode(_claims(), SECRET, algorithm="HS256")
    _, payload, signature = signed.split(".")
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )

    with pytest.raises(AuthenticationError):
        _decode(f"{header}.{payload}.{signature}", settings)


# ------------------------------------------------------- missing claims


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "typ", "jti", "iat", "exp"])
def test_a_token_missing_a_required_claim_is_refused(settings: Settings, claim: str) -> None:
    """One at a time, because a genuine token always carries all seven.

    Each of these is refused twice over - by the `require` list and again by
    the code that reads the claim - so this holds the outcome rather than
    either mechanism. Verified by gutting the `require` list and watching these
    still pass, which is why the docstring does not claim more than it can.
    `aud` is the one exception and the reason it was added to this list: a
    missing audience is caught by PyJWT's `audience=` argument, and by the
    equality check beside it, but by nothing that reads a claim out of the
    payload - so `require` is doing real work for exactly one of these seven.
    """
    payload = _claims()
    payload.pop(claim)
    token = jwt.encode(payload, SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


@pytest.mark.parametrize("issuer", ["", "wasla ", "https://evil.example", "Wasla"])
def test_a_token_from_another_issuer_is_refused(settings: Settings, issuer: str) -> None:
    """Including near-misses, since the check is equality rather than a prefix."""
    token = jwt.encode(_claims(iss=issuer), SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_a_refresh_token_is_refused_where_an_access_token_belongs(
    settings: Settings,
) -> None:
    """Otherwise a fifteen-minute lifetime becomes a fortnight's."""
    token = jwt.encode(_claims(typ=TokenType.REFRESH.value), SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


@pytest.mark.parametrize("subject", ["not-a-uuid", "", "../../etc/passwd", "1"])
def test_a_subject_that_is_not_an_identifier_is_refused(
    settings: Settings,
    subject: str,
) -> None:
    """The claim is parsed into a UUID, so anything else must fail closed.

    A `sub` that survived as a raw string would be handed to a repository
    lookup, and what that does with `../../etc/passwd` is not a question worth
    having.
    """
    token = jwt.encode(_claims(sub=subject), SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_a_tenant_claim_that_is_not_an_identifier_is_refused(settings: Settings) -> None:
    """`tid` decides which workspace is opened, so a malformed one must not pass."""
    token = jwt.encode(_claims(tid="not-a-uuid"), SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_a_version_claim_that_is_not_a_number_is_refused(settings: Settings) -> None:
    """`ver` is compared against the row to revoke sessions.

    A value that failed to parse must not become "no version", because a token
    carrying no version is handled separately and by design.
    """
    token = jwt.encode(_claims(ver="not-a-number"), SECRET, algorithm="HS256")

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_an_expired_token_is_refused_however_it_was_built(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = jwt.encode(
        _claims(exp=int(past.timestamp()), iat=int((past - timedelta(minutes=15)).timestamp())),
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationError):
        _decode(token, settings)


def test_no_refusal_says_which_check_failed(settings: Settings) -> None:
    """Every failure answers the same thing.

    A message naming the failed claim would tell somebody probing exactly which
    part of a forgery to fix next.
    """
    forgeries = [
        jwt.encode(_claims(iss="https://evil.example"), SECRET, algorithm="HS256"),
        jwt.encode(_claims(typ=TokenType.REFRESH.value), SECRET, algorithm="HS256"),
        jwt.encode(_claims(), "another-secret-entirely-long-enough", algorithm="HS256"),
        jwt.encode({k: v for k, v in _claims().items() if k != "jti"}, SECRET, algorithm="HS256"),
    ]

    messages = set()
    for token in forgeries:
        with pytest.raises(AuthenticationError) as caught:
            _decode(token, settings)
        messages.add(str(caught.value))

    assert messages == {"The credentials are not valid."}
