"""Tests for password hashing and token issuance."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.security import (
    MINIMUM_PASSWORD_LENGTH,
    TokenType,
    _create_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_invitation_token,
    hash_invitation_token,
    hash_password,
    password_needs_rehash,
    spend_verification_time,
    validate_password_strength,
    verify_password,
)

PASSWORD = "correct horse battery staple"
SUBJECT = uuid.UUID("44444444-4444-4444-4444-444444444444")
TENANT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
SHA256_HEX_LENGTH = 64


def test_hashing_is_salted_and_verifiable() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second
    assert PASSWORD not in first
    assert verify_password(password=PASSWORD, password_hash=first)
    assert verify_password(password=PASSWORD, password_hash=second)


def test_wrong_password_does_not_verify() -> None:
    password_hash = hash_password(PASSWORD)

    assert not verify_password(password="not the right phrase", password_hash=password_hash)


def test_an_unreadable_stored_hash_fails_the_login_without_raising() -> None:
    # A damaged row must fail the login, not break the endpoint.
    assert not verify_password(password=PASSWORD, password_hash="not-a-hash")
    assert password_needs_rehash("not-a-hash")


def test_current_parameters_do_not_need_rehashing() -> None:
    assert not password_needs_rehash(hash_password(PASSWORD))


@pytest.mark.parametrize(
    "candidate",
    ["short", " " * 20, "a" * (MINIMUM_PASSWORD_LENGTH - 1)],
    ids=["too-short", "only-whitespace", "one-short-of-the-minimum"],
)
def test_weak_passwords_are_rejected(candidate: str) -> None:
    with pytest.raises(ValidationError):
        validate_password_strength(candidate)


def test_absurdly_long_passwords_are_rejected() -> None:
    # Hashing cost grows with input size, so length is a denial-of-service
    # control as well as a policy.
    with pytest.raises(ValidationError):
        validate_password_strength("a" * 5000)


def test_timing_equaliser_is_safe_to_call_with_no_account() -> None:
    spend_verification_time(PASSWORD)


def test_access_token_round_trip(settings: Settings) -> None:
    token, issued = create_access_token(settings=settings, subject=SUBJECT, tenant_id=TENANT_ID)

    claims = decode_token(token, settings=settings, expected_type=TokenType.ACCESS)

    assert claims.subject == SUBJECT
    assert claims.tenant_id == TENANT_ID
    assert claims.token_type is TokenType.ACCESS
    assert claims.token_id == issued.token_id
    assert claims.seconds_until_expiry > 0


def test_refresh_token_carries_no_workspace(settings: Settings) -> None:
    token, _ = create_refresh_token(settings=settings, subject=SUBJECT)

    claims = decode_token(token, settings=settings, expected_type=TokenType.REFRESH)

    # Switching workspace must not require a new refresh token.
    assert claims.tenant_id is None


def test_each_token_has_its_own_identifier(settings: Settings) -> None:
    _, first = create_refresh_token(settings=settings, subject=SUBJECT)
    _, second = create_refresh_token(settings=settings, subject=SUBJECT)

    # Revocation is per token, so identifiers cannot repeat.
    assert first.token_id != second.token_id


def test_an_access_token_is_not_a_refresh_token(settings: Settings) -> None:
    token, _ = create_access_token(settings=settings, subject=SUBJECT)

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.REFRESH)


def test_expired_tokens_are_rejected(settings: Settings) -> None:
    # Reaching for the private helper deliberately: minting an already-expired
    # token is the only way to test the boundary.
    token, _ = _create_token(
        settings=settings,
        subject=SUBJECT,
        token_type=TokenType.ACCESS,
        lifetime=timedelta(seconds=-30),
    )

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.ACCESS)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    elsewhere = settings.model_copy(update={"jwt_secret": secrets.token_urlsafe(32)})
    token, _ = create_access_token(settings=elsewhere, subject=SUBJECT)

    with pytest.raises(AuthenticationError):
        decode_token(token, settings=settings, expected_type=TokenType.ACCESS)


def test_tampered_tokens_are_rejected(settings: Settings) -> None:
    token, _ = create_access_token(settings=settings, subject=SUBJECT)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}x.{signature}"

    with pytest.raises(AuthenticationError):
        decode_token(tampered, settings=settings, expected_type=TokenType.ACCESS)


def test_invitation_token_is_stored_only_as_a_hash() -> None:
    raw_token, stored = generate_invitation_token()

    assert stored == hash_invitation_token(raw_token)
    assert raw_token not in stored
    assert len(stored) == SHA256_HEX_LENGTH


def test_invitation_tokens_do_not_repeat() -> None:
    first_token, first_hash = generate_invitation_token()
    second_token, second_hash = generate_invitation_token()

    assert first_token != second_token
    assert first_hash != second_hash
