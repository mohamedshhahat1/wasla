"""Password reset end to end, over HTTP and against real rows.

A reset link is a credential that arrives in an inbox, so the tests here are
about what it costs to hold one and what it costs not to: the token is stored
only as a hash, it works once, a newer request kills an older link, success
ends every session, and the endpoint says exactly the same thing whether or
not the address has an account.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import hash_password, hash_reset_token
from app.db.models.email import OutboundEmail
from app.db.models.password_reset import PasswordResetToken
from app.db.models.user import User
from app.services.email_templates import EmailTemplate

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-passphrase"
EMAIL = "person@example.com"


class _Infra:
    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def reset_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
    )


@pytest.fixture
def app(reset_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app_for(reset_settings)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


def create_app_for(settings: Settings) -> FastAPI:
    from app.main import create_app

    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()
    return application


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    row = User(
        email=EMAIL,
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _request_reset(http: AsyncClient, email: str = EMAIL):
    return await http.post(f"{API}/auth/password-reset/request", json={"email": email})


async def _confirm(http: AsyncClient, token: str, password: str = NEW_PASSWORD):
    return await http.post(
        f"{API}/auth/password-reset/confirm",
        json={"token": token, "new_password": password},
    )


async def _queued_reset_token(session: AsyncSession) -> str:
    """The raw token, read out of the outbox row the request queued."""
    rows = await session.execute(
        select(OutboundEmail).where(OutboundEmail.template == EmailTemplate.PASSWORD_RESET.value)
    )
    email = rows.scalars().one()
    return email.context["token"]


async def _tokens(session: AsyncSession, user_id: uuid.UUID) -> list[PasswordResetToken]:
    rows = await session.execute(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
    )
    return list(rows.scalars())


async def test_requesting_a_reset_queues_one_email(http, db_session, user):
    response = await _request_reset(http)

    assert response.status_code == 202
    rows = await db_session.execute(
        select(OutboundEmail).where(OutboundEmail.template == EmailTemplate.PASSWORD_RESET.value)
    )
    queued = rows.scalars().all()
    assert len(queued) == 1
    assert queued[0].recipient == EMAIL
    assert queued[0].user_id == user.id


async def test_the_response_never_contains_the_token(http, db_session, user):
    """A token in an API response is a reset anybody can perform."""
    response = await _request_reset(http)
    token = await _queued_reset_token(db_session)

    assert token not in response.text


async def test_an_unknown_address_answers_exactly_the_same(http, db_session, user):
    """No enumeration oracle: registered and unknown are indistinguishable."""
    known = await _request_reset(http, EMAIL)
    unknown = await _request_reset(http, "nobody@example.com")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()  # a constant body, no request id in it


async def test_an_unknown_address_queues_nothing(http, db_session):
    await _request_reset(http, "nobody@example.com")

    rows = await db_session.execute(select(OutboundEmail))
    assert rows.scalars().all() == []


async def test_a_suspended_account_answers_the_same_and_queues_nothing(http, db_session, user):
    user.is_active = False
    await db_session.flush()

    response = await _request_reset(http)

    assert response.status_code == 202
    rows = await db_session.execute(select(OutboundEmail))
    assert rows.scalars().all() == []


async def test_an_account_with_no_password_queues_nothing(http, db_session, user):
    """An invitation-created account proves itself through its invitation."""
    user.hashed_password = None
    await db_session.flush()

    await _request_reset(http)

    rows = await db_session.execute(select(OutboundEmail))
    assert rows.scalars().all() == []


async def test_only_the_hash_of_the_token_is_stored(http, db_session, user):
    """A stolen database must not yield a usable reset link."""
    await _request_reset(http)
    raw = await _queued_reset_token(db_session)

    tokens = await _tokens(db_session, user.id)
    assert len(tokens) == 1
    assert tokens[0].token_hash != raw
    assert tokens[0].token_hash == hash_reset_token(raw)


async def test_the_token_is_long_enough_to_be_unguessable(http, db_session, user):
    await _request_reset(http)

    assert len(await _queued_reset_token(db_session)) >= 32


async def test_two_requests_produce_two_different_tokens(http, db_session, user):
    await _request_reset(http)
    first = await _queued_reset_token(db_session)
    # The outbox key is per token row, so a second request queues a second row;
    # read the newest by clearing the first.
    rows = await db_session.execute(select(OutboundEmail))
    for row in rows.scalars():
        await db_session.delete(row)
    await db_session.flush()
    await _request_reset(http)
    second = await _queued_reset_token(db_session)

    assert first != second


async def test_confirming_sets_the_new_password(http, db_session, user):
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    previous = user.hashed_password

    response = await _confirm(http, token)

    assert response.status_code == 200
    await db_session.refresh(user)
    assert user.hashed_password != previous


async def test_confirming_bumps_the_token_version(http, db_session, user):
    """Every access and refresh token dies with the old password (ADR-036)."""
    before = user.token_version
    await _request_reset(http)
    token = await _queued_reset_token(db_session)

    await _confirm(http, token)

    await db_session.refresh(user)
    assert user.token_version == before + 1


async def test_the_new_password_actually_signs_in(http, db_session, user):
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    await _confirm(http, token)

    response = await http.post(
        f"{API}/auth/login",
        json={"email": EMAIL, "password": NEW_PASSWORD},
    )

    assert response.status_code == 200


async def test_the_old_password_stops_working(http, db_session, user):
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    await _confirm(http, token)

    response = await http.post(
        f"{API}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )

    assert response.status_code == 401


async def test_a_token_cannot_be_used_twice(http, db_session, user):
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    await _confirm(http, token)

    second = await _confirm(http, token, "yet-another-passphrase")

    assert second.status_code == 401


async def test_an_expired_token_is_refused(http, db_session, user):
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    rows = await _tokens(db_session, user.id)
    rows[0].expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    response = await _confirm(http, token)

    assert response.status_code == 401


async def test_a_newer_request_supersedes_the_older_link(http, db_session, user):
    """Asking twice narrows the live surface rather than widening it."""
    await _request_reset(http)
    first = await _queued_reset_token(db_session)
    rows = await db_session.execute(select(OutboundEmail))
    for row in rows.scalars():
        await db_session.delete(row)
    await db_session.flush()
    await _request_reset(http)

    response = await _confirm(http, first)

    assert response.status_code == 401


async def test_an_unknown_token_is_refused(http, db_session, user):
    response = await _confirm(http, "a" * 43)

    assert response.status_code == 401


async def test_every_refusal_reads_the_same(http, db_session, user):
    """Unknown, expired and spent tokens must not be distinguishable."""
    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    await _confirm(http, token)

    spent = await _confirm(http, token, "another-passphrase-here")
    unknown = await _confirm(http, "b" * 43, "another-passphrase-here")

    assert spent.status_code == unknown.status_code
    # The request id differs per request by design; the refusal must not.
    assert spent.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert spent.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_a_weak_password_is_refused_without_spending_the_token(http, db_session, user):
    """A typo must not cost the one usable link."""
    await _request_reset(http)
    token = await _queued_reset_token(db_session)

    weak = await _confirm(http, token, "short")
    assert weak.status_code in (400, 422)

    good = await _confirm(http, token)
    assert good.status_code == 200


async def test_confirming_queues_a_password_changed_notice(http, db_session, user):
    """The notice a compromised account depends on."""
    await _request_reset(http)
    token = await _queued_reset_token(db_session)

    await _confirm(http, token)

    rows = await db_session.execute(
        select(OutboundEmail).where(OutboundEmail.template == EmailTemplate.PASSWORD_CHANGED.value)
    )
    notices = rows.scalars().all()
    assert len(notices) == 1
    assert notices[0].recipient == EMAIL


async def test_the_notice_goes_to_the_account_not_the_request(http, db_session, user):
    """Nothing in the request can redirect where a security notice lands."""
    await _request_reset(http)
    token = await _queued_reset_token(db_session)

    await http.post(
        f"{API}/auth/password-reset/confirm",
        json={
            "token": token,
            "new_password": NEW_PASSWORD,
            "email": "attacker@evil.test",
        },
    )

    rows = await db_session.execute(select(OutboundEmail))
    recipients = {row.recipient for row in rows.scalars()}
    assert recipients == {EMAIL}


async def test_no_reset_token_reaches_the_audit_trail(http, db_session, user):
    from app.db.models.audit import AuditLog

    await _request_reset(http)
    token = await _queued_reset_token(db_session)
    await _confirm(http, token)

    rows = await db_session.execute(select(AuditLog))
    for entry in rows.scalars():
        assert token not in str(entry.meta or {})
        assert token not in str(entry.target_label or "")


async def test_the_address_is_matched_case_insensitively(http, db_session, user):
    response = await _request_reset(http, "PERSON@EXAMPLE.COM")

    assert response.status_code == 202
    rows = await db_session.execute(select(OutboundEmail))
    assert len(rows.scalars().all()) == 1
