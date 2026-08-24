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
