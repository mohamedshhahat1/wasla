"""Password hashing and token issuance.

This module is pure cryptography and claim handling: it touches neither the
database nor Redis. That keeps the security rules in one directly testable
place, independent of how any particular request arrives.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ValidationError

ISSUER: Final = "wasla"
MINIMUM_PASSWORD_LENGTH: Final = 12
# Argon2 imposes no length limit of its own, but hashing cost grows with input
# size, so an unbounded password is a denial-of-service vector.
MAXIMUM_PASSWORD_LENGTH: Final = 128
INVITATION_TOKEN_BYTES: Final = 32
RESET_TOKEN_BYTES: Final = 32

# Length of an email verification code. Six digits is a million values, which
# is small enough that the attempt cap and the rate limit - not the keyspace -
# are what make guessing hopeless.
VERIFICATION_CODE_DIGITS: Final = 6
_VERIFICATION_CODE_SPACE: Final = 10**VERIFICATION_CODE_DIGITS

_hasher = PasswordHasher()


class TokenType(StrEnum):
    """Kind of token, carried in the payload so one cannot pass as the other."""

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Claims of a token this application issued and has verified."""

    subject: uuid.UUID
    token_type: TokenType
    token_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime
    tenant_id: uuid.UUID | None = None
    # The value of `users.token_version` when this token was minted (ADR-036).
    # Optional only so a token issued before the column existed decodes rather
    # than raising; such a token is treated as stale by the checks that read it.
    token_version: int | None = None

    @property
    def seconds_until_expiry(self) -> int:
        """Remaining lifetime, floored at zero.

        A revocation entry only needs to outlive the token it revokes, so this
        is the natural time-to-live for one.
        """
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds()
        return max(int(remaining), 0)


def validate_password_strength(password: str) -> None:
    """Reject passwords policy does not allow.

    Length is the only rule. Composition rules push people towards predictable
    substitutions, whereas length is what actually costs an attacker.
    """
    if len(password.strip()) < MINIMUM_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MINIMUM_PASSWORD_LENGTH} characters long.",
        )
    if len(password) > MAXIMUM_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at most {MAXIMUM_PASSWORD_LENGTH} characters long.",
        )


def hash_password(password: str) -> str:
    """Hash a password with Argon2id.

    The salt, the algorithm and the cost parameters all live inside the returned
    string, so a stored hash carries everything needed to verify it and to
    notice later that policy has moved on.
    """
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(*, password: str, password_hash: str) -> bool:
    """Whether the password matches.

    Never raises. A wrong password and an unreadable stored value are both a
    failed login, not an error the endpoint should surface.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, ValueError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash is weaker than current policy.

    Raising cost parameters is only useful if existing hashes get upgraded, so
    callers rehash on the next successful login. An unreadable hash counts as
    needing replacement.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (Argon2Error, ValueError):
        return True


@lru_cache(maxsize=1)
def _unusable_hash() -> str:
    """Hash of a random value nobody knows."""
    return _hasher.hash(secrets.token_urlsafe(32))


def spend_verification_time(password: str) -> None:
    """Do the work of a verification that cannot succeed.

    Login must not reveal whether an email address has an account, and response
    time is a side channel like any other. Callers use this when no user
    matches, or when the user has no password set.
    """
    verify_password(password=password, password_hash=_unusable_hash())


def _create_token(
    *,
    settings: Settings,
    subject: uuid.UUID,
    token_type: TokenType,
    lifetime: timedelta,
    tenant_id: uuid.UUID | None = None,
    token_version: int | None = None,
) -> tuple[str, TokenClaims]:
    issued_at = datetime.now(UTC)
    expires_at = issued_at + lifetime
    token_id = uuid.uuid4()

    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": str(subject),
        "typ": token_type.value,
        "jti": str(token_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
    if token_version is not None:
        # Short name for the same reason the others are short: it rides in every
        # request header, and a JWT is not a place to be verbose.
        payload["ver"] = int(token_version)

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    claims = TokenClaims(
        subject=subject,
        token_type=token_type,
        token_id=token_id,
        issued_at=issued_at,
        expires_at=expires_at,
        tenant_id=tenant_id,
        token_version=token_version,
    )
    return token, claims


def create_access_token(
    *,
    settings: Settings,
    subject: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    token_version: int | None = None,
) -> tuple[str, TokenClaims]:
    """Mint a short-lived access token.

    The active workspace travels inside the token, so a request always states
    which tenant it is scoped to and no route has to trust a tenant id from
    request input.
    """
    return _create_token(
        settings=settings,
        subject=subject,
        token_type=TokenType.ACCESS,
        lifetime=timedelta(seconds=settings.access_token_ttl_seconds),
        tenant_id=tenant_id,
        token_version=token_version,
    )


def create_refresh_token(
    *,
    settings: Settings,
    subject: uuid.UUID,
    token_version: int | None = None,
) -> tuple[str, TokenClaims]:
    """Mint a long-lived refresh token.

    No workspace is embedded: which tenant to open is decided when an access
    token is minted, so switching workspace never requires a new refresh token.
    """
    return _create_token(
        settings=settings,
        subject=subject,
        token_type=TokenType.REFRESH,
        lifetime=timedelta(seconds=settings.refresh_token_ttl_seconds),
        token_version=token_version,
    )


def decode_token(token: str, *, settings: Settings, expected_type: TokenType) -> TokenClaims:
    """Verify a token and return its claims.

    Every failure looks the same to the caller: the credentials are not valid.
    Which check failed stays out of the response.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=ISSUER,
            options={"require": ["iss", "sub", "typ", "jti", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise AuthenticationError("The session has expired.") from error
    except jwt.InvalidTokenError as error:
        raise AuthenticationError("The credentials are not valid.") from error

    if payload.get("typ") != expected_type.value:
        # An access token must never be accepted where a refresh token belongs,
        # or its short lifetime stops meaning anything.
        raise AuthenticationError("The credentials are not valid.")

    try:
        return TokenClaims(
            subject=uuid.UUID(str(payload["sub"])),
            token_type=expected_type,
            token_id=uuid.UUID(str(payload["jti"])),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            tenant_id=uuid.UUID(str(payload["tid"])) if payload.get("tid") else None,
            token_version=int(payload["ver"]) if payload.get("ver") is not None else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AuthenticationError("The credentials are not valid.") from error


def hash_invitation_token(raw_token: str) -> str:
    """Hash an invitation token for storage and lookup.

    SHA-256 rather than Argon2 on purpose: the token is 256 bits of randomness,
    so there is nothing to brute-force and a deliberately slow hash would only
    slow down lookups.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_invitation_token() -> tuple[str, str]:
    """Return the token to send, and the hash to store.

    Only the hash is persisted, so a leaked database cannot be used to accept
    invitations: the value that opens one exists solely in the message sent to
    the invitee.
    """
    raw_token = secrets.token_urlsafe(INVITATION_TOKEN_BYTES)
    return raw_token, hash_invitation_token(raw_token)


def hash_reset_token(raw_token: str) -> str:
    """Hash a password reset token for storage and lookup.

    SHA-256 for the invitation token's reason: 256 bits of randomness leave
    nothing to brute-force, so a deliberately slow hash would only slow the
    lookup. The hash is what the unique index compares, which also means the
    comparison cost never depends on how much of a guessed token matches.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_reset_token() -> tuple[str, str]:
    """Return the reset token to email, and the hash to store.

    Only the hash is persisted (ADR-042). A stolen database cannot reset
    anybody's password: the value that can exists solely in the message sent
    to the address on file, which is the proof of ownership the whole flow
    rests on.
    """
    raw_token = secrets.token_urlsafe(RESET_TOKEN_BYTES)
    return raw_token, hash_reset_token(raw_token)


def hash_verification_code(code: str) -> str:
    """Hash an email verification code for storage.

    Argon2, and this is the one place that departs from the two functions above
    - so the reason matters. They are SHA-256 *because* their input is 256 bits
    of randomness: nothing to brute-force, and slowness would only tax the
    lookup. A six-digit code inverts every part of that. Its keyspace is a
    million values, so a leaked database of SHA-256 digests is not a database of
    unguessable secrets - it is a database of codes recoverable in the time it
    takes to hash a million short strings, which is not long enough to matter.

    Argon2 makes each candidate cost real work, which is the only thing that
    makes a twenty-bit secret survive disclosure of its verifier.

    The cost is that this cannot be a lookup key. Argon2 salts every hash, so
    the same code hashes differently each time and no index can be built on it;
    the challenge is located by account and this only ever confirms or denies.
    """
    return _hasher.hash(code)


def verify_verification_code(*, code: str, code_hash: str) -> bool:
    """Whether the submitted code matches the stored verifier.

    Never raises, for `verify_password`'s reason: a wrong code and an
    unreadable stored value are both a failed attempt. Argon2's own comparison
    does the work, so there is no hand-rolled equality here to get wrong.
    """
    try:
        return _hasher.verify(code_hash, code)
    except (Argon2Error, ValueError):
        return False


def generate_verification_code() -> tuple[str, str]:
    """Return the six-digit code to email, and the hash to store.

    `secrets` rather than `random`, because a code produced by a predictable
    generator is not a secret at all - and it is derived from nothing: not the
    account, not the address, not the clock. Anything derived from those is
    reproducible by whoever knows them.

    Formatted to width rather than assembled arithmetically, so `000042` is a
    perfectly ordinary code. Building digits by division tends to drop leading
    zeroes, which quietly removes a tenth of the keyspace for each one lost.

    Only the hash is persisted. The plaintext returned here has exactly one
    legitimate destination - the message sent to the address being proven - and
    must reach no log, no audit entry, no response body and no URL.
    """
    code = f"{secrets.randbelow(_VERIFICATION_CODE_SPACE):0{VERIFICATION_CODE_DIGITS}d}"
    return code, hash_verification_code(code)


def normalise_verification_code(submitted: str) -> str | None:
    """Clean up a submitted code, or `None` if it cannot be one.

    Spaces and hyphens are stripped because people paste `482 731` out of a
    mail client, and refusing that teaches them the product is broken rather
    than that they mistyped.

    The digit test is deliberately ASCII-only. `"\u0661\u0662\u0663\u0664\u0665\u0666".isdigit()` is `True`
    in Python and `int()` parses it happily, and this product's users type
    Arabic. A check written as `.isdigit()` alone would accept Arabic-Indic
    numerals, hash them, compare against a verifier built from Western digits,
    and fail - burning an attempt and looking, to the person holding the right
    code, exactly like a broken system. Rejecting them here means the caller is
    told the format is wrong instead.

    Returning `None` rather than raising keeps the decision with the caller,
    which needs malformed input to look identical to a wrong code.
    """
    candidate = submitted.strip().replace(" ", "").replace("-", "")
    if len(candidate) != VERIFICATION_CODE_DIGITS:
        return None
    if not (candidate.isascii() and candidate.isdigit()):
        return None
    return candidate


def spend_code_verification_time(code: str) -> None:
    """Do the work of a code check that cannot succeed.

    `spend_verification_time` for verification codes. Used when the account has
    no live challenge, so that "nothing outstanding" and "wrong code" take the
    same time to answer. A small oracle, but Argon2 is slow enough that its
    absence would be plainly measurable.
    """
    verify_verification_code(code=code, code_hash=_unusable_hash())
