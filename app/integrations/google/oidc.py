"""Cryptographic validation of Google ID tokens.

This module is the trust boundary. Everything upstream of it is a string a
stranger sent us; everything downstream may be believed. Nothing else in the
application is allowed to decide that a Google token is genuine.

**The algorithm is a literal, and this module never reads
`settings.jwt_algorithm`.** That separation is the entire defence against
algorithm confusion, and it is worth stating why it takes this shape rather
than reusing `app/core/security.py`. Wasla's own tokens are HMAC over a shared
secret; Google's are RSA over a key Google publishes. If one verifier accepted
both families, then anybody who learned the *public* key could sign a token
that the HMAC branch would accept as authentic - the classic confusion. So
there are two verifiers with no shared code, no shared key material, different
issuers and different required claims, and a Wasla token presented here is as
unacceptable as a Google token presented there.

**Reading the header is not trusting the token.** Choosing a key requires
reading `kid`, which means parsing the JOSE header of something unverified.
That is safe and it is not a claim: `alg` is compared against a literal, `kid`
is only ever used to look up a *published public key*, and no claim in the
payload is read until the signature over it has been checked. Anybody tempted
to read `email` a few lines earlier should read this paragraph instead.

**Keys are cached with three ages, not one TTL.** Fresh keys are used. Keys
past the fresh window but inside the stale window are still used when Google
cannot be reached, which is what carries a login through a blip. Keys past the
stale window are refused, because a key that old could have been withdrawn
without this process ever hearing about it, and "the cache is ancient" is not a
reason to accept a signature. Refresh attempts are throttled, so a stream of
forged tokens carrying random key ids cannot turn this process into a load
generator pointed at Google.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
import jwt
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
    PyJWKSetError,
)

from app.core.logging import get_logger
from app.core.net import UnsafeUrlError, build_guarded_client

logger = get_logger(__name__)

# Fixed, and not configurable. A client-supplied key document URL is the whole
# attack: point it somewhere you control and every signature verifies.
GOOGLE_JWKS_URL: Final = "https://www.googleapis.com/oauth2/v3/certs"

# Google issues *both* of these as `iss`, which is why this is a set and why the
# check below is written by hand. PyJWT's `issuer=` parameter takes a single
# string and cannot express "either of these two", so delegating to it would
# mean silently refusing half of Google's tokens - or, worse, being loosened to
# accept anything the first time somebody hit that.
GOOGLE_ISSUERS: Final = frozenset({"https://accounts.google.com", "accounts.google.com"})

# `Final = "RS256"` is an algorithm name, not a credential; S105 matches on "TOKEN"
# appearing in the identifier.
ID_TOKEN_ALGORITHM: Final = "RS256"  # noqa: S105

# Absent any of these, there is nothing to reason about. `nonce` is not in the
# list because its absence gets its own refusal reason, which is worth
# distinguishing in the logs from a token that is merely malformed.
_REQUIRED_CLAIMS: Final = ("iss", "aud", "exp", "iat", "sub")

# Enough for a container whose clock has drifted a little, not enough to matter
# to an expired token. Applied to `exp` and `iat` alike.
CLOCK_SKEW_SECONDS: Final = 30

JWKS_TIMEOUT_SECONDS: Final = 5.0
# Past this, a refresh is attempted before the cache is used.
JWKS_FRESH_SECONDS: Final = 3_600
# Past this, the cache is not used at all even if Google is unreachable.
JWKS_STALE_SECONDS: Final = 86_400
# At most one fetch attempt per this interval, successful or not.
JWKS_MIN_REFRESH_SECONDS: Final = 60
# Google's key document is a couple of kilobytes. The cap is not about Google;
# it is about what arrives when a resolver has been hijacked, which is the
# scenario `build_guarded_client` exists for.
MAX_JWKS_BYTES: Final = 64 * 1024


class GoogleTokenInvalidError(Exception):
    """The token was not acceptable.

    `reason` is a short stable category for logs and for the audit trail. It is
    never returned to the caller: telling somebody *which* check their forgery
    failed is telling them how to write a better one. Deliberately not a
    `WaslaError`, so it cannot be mapped to an HTTP response by accident - a
    refusal here has to be translated on purpose (the same reasoning
    `app/core/net.py` gives for `UnsafeUrlError`).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GoogleKeysUnavailableError(Exception):
    """Google's signing keys could not be obtained, so nothing can be verified.

    Distinct from :class:`GoogleTokenInvalidError` because the two mean opposite
    things about the caller. An invalid token is somebody's problem to fix; an
    unavailable key set is ours, and it must not be reported as a rejected
    login.
    """


@dataclass(frozen=True, slots=True)
class GoogleIdentityClaims:
    """What a validated Google ID token says about a person.

    Frozen, because the point of this type is that its contents have been
    proven. Something that can be mutated after validation is something that
    can be mutated *instead of* validation.
    """

    subject: str
    email: str
    email_verified: bool
    full_name: str | None


class GoogleKeyRing:
    """Google's published signing keys, cached with a bounded refresh.

    One instance per process, shared by every request. Google rotates keys
    without notice, so nothing here is pinned and an unknown key id triggers a
    single refresh rather than a failure - which is what makes a rotation
    invisible apart from one cache miss.
    """

    def __init__(self, *, jwks_url: str = GOOGLE_JWKS_URL) -> None:
        self._jwks_url = jwks_url
        self._keys: dict[str, jwt.PyJWK] = {}
        # `monotonic`, not wall clock: an NTP correction must not make the cache
        # look a day old, and must not make an expired one look fresh.
        self._fetched_at: float | None = None
        self._last_attempt_at: float | None = None
        # Serialises refreshes so that a burst of requests arriving during a
        # rotation produces one fetch rather than one per request.
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> jwt.PyJWK:
        """The published key with this id, refreshing once if it is unknown."""
        if not self._needs_refresh(kid):
            cached = self._usable(kid)
            if cached is not None:
                return cached

        async with self._lock:
            # Re-checked under the lock: while this request waited, another may
            # have fetched exactly the key it is missing.
            if self._needs_refresh(kid) and self._refresh_allowed():
                try:
                    await self._refresh()
                except GoogleKeysUnavailableError:
                    # The stale-cache policy. A fetch failure is only fatal when
                    # there is nothing usable to fall back on, so a Google blip
                    # does not take sign-in down - but a cache old enough that a
                    # withdrawn key might still be in it is not a fallback.
                    if self._usable(kid) is None:
                        raise
                    logger.warning(
                        "google.jwks_serving_stale",
                        extra={
                            "event": "google.jwks_serving_stale",
                            "age_seconds": self._age_seconds(),
                        },
                    )
            key = self._usable(kid)

        if key is None:
            # Either a forgery, or a key Google has not published. Both are the
            # caller's problem and neither is worth another fetch.
            raise GoogleTokenInvalidError("unknown_key_id")
        return key

    def _age_seconds(self) -> float | None:
        if self._fetched_at is None:
            return None
        return time.monotonic() - self._fetched_at

    def _usable(self, kid: str) -> jwt.PyJWK | None:
        """A cached key, if one is held and the cache is not too old to trust."""
        age = self._age_seconds()
        if age is None or age > JWKS_STALE_SECONDS:
            return None
        return self._keys.get(kid)

    def _needs_refresh(self, kid: str) -> bool:
        age = self._age_seconds()
        if age is None:
            return True
        if kid not in self._keys:
            return True
        return age > JWKS_FRESH_SECONDS

    def _refresh_allowed(self) -> bool:
        """Whether another fetch may be attempted yet.

        The bound is what stops a stream of tokens carrying random key ids from
        making this process hammer Google on an attacker's schedule. The cost is
        that a genuine rotation can take up to this long to be picked up, during
        which some logins fail - a trade worth making, because the alternative
        is a denial-of-service amplifier pointed at our own dependency.
        """
        if self._last_attempt_at is None:
            return True
        return time.monotonic() - self._last_attempt_at >= JWKS_MIN_REFRESH_SECONDS

    async def _refresh(self) -> None:
        # Recorded before the attempt, not after, so a hanging fetch still
        # counts as an attempt and cannot be retried in a tight loop.
        self._last_attempt_at = time.monotonic()
        payload = await self._fetch()

        try:
            key_set = jwt.PyJWKSet.from_dict(payload)
        except (PyJWKSetError, InvalidTokenError, ValueError, TypeError, KeyError) as exc:
            # A malformed document, or one whose every key is unusable. Caught
            # broadly on purpose: this is parsing input from the network, and a
            # surprise here must be a refused login rather than a 500.
            logger.warning(
                "google.jwks_unusable",
                extra={"event": "google.jwks_unusable", "reason": type(exc).__name__},
            )
            raise GoogleKeysUnavailableError("Google published no usable signing keys.") from exc

        keys = {key.key_id: key for key in key_set.keys if key.key_id}
        if not keys:
            logger.warning(
                "google.jwks_unidentified",
                extra={"event": "google.jwks_unidentified"},
            )
            raise GoogleKeysUnavailableError("Google published no identified signing keys.")

        # Replaced wholesale rather than merged. Merging would keep a withdrawn
        # key working forever, which is the one thing a key set must not do.
        self._keys = keys
        self._fetched_at = time.monotonic()
        logger.info(
            "google.jwks_refreshed",
            extra={"event": "google.jwks_refreshed", "key_count": len(keys)},
        )

    async def _fetch(self) -> dict[str, Any]:
        body = bytearray()
        try:
            async with (
                build_guarded_client(timeout=httpx.Timeout(JWKS_TIMEOUT_SECONDS)) as client,
                client.stream("GET", self._jwks_url) as response,
            ):
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        # Read incrementally so an endless response is abandoned
                        # rather than buffered. Checking Content-Length would
                        # trust the sender about how much it intends to send.
                        raise GoogleKeysUnavailableError("Google's key document was too large.")
        except GoogleKeysUnavailableError:
            raise
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            # Includes the timeout, and includes a resolver answering with a
            # private address - which `build_guarded_client` refuses outright.
            logger.warning(
                "google.jwks_unreachable",
                extra={"event": "google.jwks_unreachable", "reason": type(exc).__name__},
            )
            raise GoogleKeysUnavailableError("Google's signing keys could not be fetched.") from exc

        try:
            payload = json.loads(bytes(body))
        except json.JSONDecodeError as exc:
            raise GoogleKeysUnavailableError("Google's key document was not JSON.") from exc
        if not isinstance(payload, dict):
            raise GoogleKeysUnavailableError("Google's key document was not an object.")
        return payload


class GoogleIdTokenVerifier:
    """Turns a string that claims to be from Google into claims that are.

    Knows nothing about Wasla's users, sessions or database. It answers exactly
    one question - is this token genuine, current, addressed to us, and bound to
    the authorization attempt we started - and refuses to answer any other.
    """

    def __init__(self, *, client_id: str, key_ring: GoogleKeyRing) -> None:
        self._client_id = client_id
        self._key_ring = key_ring

    async def verify(self, *, id_token: str, nonce: str) -> GoogleIdentityClaims:
        """Validate an ID token and return what it proves.

        :param nonce: the value this flow sent to Google. Comparing it here is
            what makes a token replayed from another flow useless.
        :raises GoogleTokenInvalidError: the token is not acceptable, for any reason.
        :raises GoogleKeysUnavailableError: nothing could be verified either way.
        """
        header = self._header(id_token)

        # Checked before a key is even looked up, so `alg: none` and an HS256
        # token wearing Google's clothes are refused without a network call.
        # `jwt.decode` would refuse them too - `algorithms` is a literal
        # one-element list - but refusing here keeps the JWKS cache out of reach
        # of anything that was never going to verify.
        if header.get("alg") != ID_TOKEN_ALGORITHM:
            raise GoogleTokenInvalidError("unexpected_algorithm")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise GoogleTokenInvalidError("missing_key_id")

        key = await self._key_ring.key_for(kid)
        payload = self._decode(id_token, key=key)

        # From here the signature is verified, so - and only so - the claims can
        # be read. `aud` and `exp` were checked inside `_decode`.
        self._check_issuer(payload)
        self._check_nonce(payload, expected=nonce)
        return self._extract(payload)

    @staticmethod
    def _header(id_token: str) -> dict[str, Any]:
        """Read the JOSE header in order to choose a key.

        Not a trust decision. Nothing read here is believed: `alg` is compared
        against a literal and `kid` only ever indexes a key Google published.
        """
        try:
            return jwt.get_unverified_header(id_token)
        except InvalidTokenError as exc:
            raise GoogleTokenInvalidError("malformed") from exc

    def _decode(self, id_token: str, *, key: jwt.PyJWK) -> dict[str, Any]:
        """Verify the signature, the audience and the clock.

        `algorithms` is a one-element literal list. That single argument is what
        refuses an unsigned token, `alg: none`, and an HMAC token signed with
        something the attacker knows - PyJWT will not consider an algorithm that
        is not in it, whatever the header says.

        `audience` is the configured client id, so a token Google minted for a
        different application is refused even though it is genuinely Google's.
        """
        try:
            return jwt.decode(
                id_token,
                key=key.key,
                algorithms=[ID_TOKEN_ALGORITHM],
                audience=self._client_id,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except ExpiredSignatureError as exc:
            raise GoogleTokenInvalidError("expired") from exc
        except InvalidAudienceError as exc:
            raise GoogleTokenInvalidError("wrong_audience") from exc
        except InvalidSignatureError as exc:
            raise GoogleTokenInvalidError("bad_signature") from exc
        except MissingRequiredClaimError as exc:
            raise GoogleTokenInvalidError("missing_claim") from exc
        except InvalidTokenError as exc:
            # The catch-all, and it must stay last: every exception above is a
            # subclass of this one.
            logger.info(
                "google.id_token_rejected",
                extra={
                    "event": "google.id_token_rejected",
                    "reason": type(exc).__name__,
                },
            )
            raise GoogleTokenInvalidError("invalid_token") from exc

    @staticmethod
    def _check_issuer(payload: dict[str, Any]) -> None:
        """Only Google, and only the two spellings Google actually uses."""
        if payload.get("iss") not in GOOGLE_ISSUERS:
            raise GoogleTokenInvalidError("wrong_issuer")

    @staticmethod
    def _check_nonce(payload: dict[str, Any], *, expected: str) -> None:
        """Bind the token to the authorization attempt that asked for it.

        Without this, a token obtained in any flow for this client would be
        accepted in any other - which is the replay the nonce exists to stop.
        `compare_digest` because it costs nothing and reads as intended; the
        timing channel on a short-lived per-flow value is thin.
        """
        presented = payload.get("nonce")
        if not isinstance(presented, str) or not presented:
            raise GoogleTokenInvalidError("missing_nonce")
        if not hmac.compare_digest(presented, expected):
            raise GoogleTokenInvalidError("wrong_nonce")

    @staticmethod
    def _extract(payload: dict[str, Any]) -> GoogleIdentityClaims:
        """Pull out the four things this application needs, strictly.

        Types are checked rather than coerced. `email_verified` in particular is
        compared against `True` and not evaluated for truthiness: the string
        "false" is truthy in Python, and a provider that sent one would
        otherwise be read as having verified the address.
        """
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise GoogleTokenInvalidError("missing_subject")

        email = payload.get("email")
        if not isinstance(email, str) or not email.strip():
            # Required because every downstream decision - create, collide, or
            # record a verification - is about an address. A token without one
            # cannot be acted on, so it is refused rather than half-handled.
            raise GoogleTokenInvalidError("missing_email")

        full_name = payload.get("name")
        if not isinstance(full_name, str) or not full_name.strip():
            full_name = None

        return GoogleIdentityClaims(
            subject=subject.strip(),
            email=email.strip(),
            email_verified=payload.get("email_verified") is True,
            full_name=full_name.strip() if full_name else None,
        )
