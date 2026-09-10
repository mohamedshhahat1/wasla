"""Committed PostgreSQL races on the two conditional consumes.

Password reset and email verification both end with a conditional ``UPDATE``
whose *result* is the security decision - ``if not await self._tokens.consume``
and ``if not await self._challenges.consume``. Both are the ADR-039 shape and
both are correct. Neither had a test that could tell them apart from a
check-then-act implementation, because sequential replay is caught earlier by
``is_usable`` and the concurrent branch is the only one the guard exists for:
ignoring the consume result left the suite green in both cases (AUTH-02).

``test_lifecycle_concurrency.py`` already races two confirmations of one reset
token. What is here is wider and covers the half that had nothing at all:

* **four** reset redemptions, each proposing a *different* new password, so
  "exactly one winner" is checked by which password actually signs in rather
  than only by a status code - two winners writing the same string would be
  indistinguishable from one;
* the sessions the reset was supposed to end, checked afterwards;
* **five** verification submissions of one correct code, so exactly one 200,
  one ``consumed_at`` and one ``email_verified`` entry.

Every test drives real applications over real connections that commit
independently - ``db_session``'s outer transaction cannot represent two
requests that commit - and every one is synchronised with an ``asyncio.Barrier``
planted inside the code under test, between the read and the write the guard
adjudicates. Rows are uniquely named and removed in a ``finally``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import (
    generate_reset_token,
    generate_verification_code,
    hash_password,
)
from app.db.models import User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.email import OutboundEmail
from app.db.models.email_verification import EmailVerificationChallenge
from app.db.models.password_reset import PasswordResetToken
from app.db.session import Database
from app.main import create_app
from app.repositories.email_verification_repository import EmailVerificationRepository
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"

# Four redemptions, four distinct candidates. Distinct is the point: if two
# requests both won while proposing the same password, the account would end up
# in a state no assertion could distinguish from one request winning.
CANDIDATES = (
    "the first candidate passphrase",
    "the second candidate passphrase",
    "the third candidate passphrase",
    "the fourth candidate passphrase",
)

# Five submissions of one correct code. The service counts an attempt before
# comparing, and `consume` allows `attempts <= max_attempts`, so five
# contenders sit exactly on the default ceiling - which is deliberate: it
# proves the winner is decided by the consume and not by the cap refusing four
# of them on the way in.
SUBMISSIONS = 5


class _Redis:
    """One Redis for every racing application, as a deployment has."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        async with self._lock:
            if nx and key in self.values:
                return None
            self.values[key] = value
            return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None

    @property
    def client(self) -> _Redis:
        return self


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        credential_encryption_keys=[TEST_CREDENTIAL_ENCRYPTION_KEY],
    )


@asynccontextmanager
async def _client(database_url: str, redis: _Redis) -> AsyncIterator[AsyncClient]:
    settings = _settings(database_url)
    application = create_app(settings)
    database = Database(settings)
    application.state.database = database
    application.state.redis = redis
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://wasla.test",
        ) as client:
            yield client
    finally:
        await database.dispose()


@asynccontextmanager
async def _sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database = Database(_settings(database_url))
    try:
        yield async_sessionmaker(database.engine, expire_on_commit=False)
    finally:
        await database.dispose()


async def _seed_user(
    session: AsyncSession,
    *,
    email: str,
    verified: bool = True,
) -> User:
    user = User(
        email=email,
        full_name="Race Participant",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(user)
    await session.flush()
    return user


async def _cleanup(maker: async_sessionmaker[AsyncSession], *, emails: list[str]) -> None:
    async with maker() as session:
        await session.execute(delete(OutboundEmail).where(OutboundEmail.recipient.in_(emails)))
        user_ids = list(
            (await session.execute(select(User.id).where(User.email.in_(emails)))).scalars()
        )
        if user_ids:
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await session.execute(delete(User).where(User.email.in_(emails)))
        await session.commit()


# ------------------------------------------------------------- password reset


async def test_four_redemptions_of_one_reset_token_leave_one_usable_password(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One winner, and the other three passwords must open nothing.

    The barrier sits between the read that finds the token usable and the
    conditional consume that spends it, so all four requests hold a token they
    have already decided is good. That is the window the guard exists for, and
    the only one in which ignoring its result changes the outcome.

    Four different candidate passwords make the winner identifiable. Status
    codes alone would not: a second winner overwriting the first with the same
    string looks exactly like one winner.
    """
    suffix = uuid.uuid4().hex[:10]
    email = f"reset-race4-{suffix}@example.com"
    redis = _Redis()
    raw_token, token_hash = generate_reset_token()
    barrier = asyncio.Barrier(len(CANDIDATES))
    original = PasswordResetTokenRepository.get_by_token_hash

    async def synchronised_read(
        self: PasswordResetTokenRepository,
        token_hash_value: str,
    ) -> PasswordResetToken | None:
        row = await original(self, token_hash_value)
        await barrier.wait()
        return row

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = await _seed_user(session, email=email)
                session.add(
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash=token_hash,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                )
                await session.commit()
                user_id = user.id
                starting_version = user.token_version

            # A live session from before the reset, so the revocation the reset
            # promises is checked against a credential that actually worked.
            async with _client(prepared_database, redis) as client:
                signed_in = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": PASSWORD}
                )
                assert signed_in.status_code == 200, signed_in.text
                old_access = signed_in.json()["access_token"]
                old_refresh = signed_in.json()["refresh_token"]

            monkeypatch.setattr(
                PasswordResetTokenRepository, "get_by_token_hash", synchronised_read
            )

            async def confirm(candidate: str) -> int:
                async with _client(prepared_database, redis) as client:
                    response = await client.post(
                        f"{API}/auth/password-reset/confirm",
                        json={"token": raw_token, "new_password": candidate},
                    )
                    return response.status_code

            statuses = await asyncio.gather(*(confirm(value) for value in CANDIDATES))
            monkeypatch.undo()

            # Exactly one success and three controlled refusals. Nothing 500s:
            # losing this race is an ordinary outcome, not an error.
            assert sorted(statuses) == [200, 401, 401, 401], statuses

            async with maker() as session:
                token_row = await session.scalar(
                    select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
                )
                assert token_row is not None
                assert token_row.consumed_at is not None
                refreshed = await session.get(User, user_id)
                assert refreshed is not None
                # One bump. Two winners would show as two, and would leave the
                # account on a version an outstanding token still matched.
                assert refreshed.token_version == starting_version + 1
                # One completion in the trail, not four.
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.actor_id == user_id,
                            AuditLog.action == AuditAction.PASSWORD_RESET_COMPLETED,
                        )
                    )
                    == 1
                )

            async with _client(prepared_database, redis) as client:
                accepted = []
                for candidate in CANDIDATES:
                    response = await client.post(
                        f"{API}/auth/login", json={"email": email, "password": candidate}
                    )
                    assert response.status_code in (200, 401), response.text
                    if response.status_code == 200:
                        accepted.append(candidate)
                # Exactly one of the four opens the account, and the original
                # password does not.
                assert len(accepted) == 1, accepted
                stale = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": PASSWORD}
                )
                assert stale.status_code == 401

                # And every credential minted before the reset is dead.
                whoami = await client.get(
                    f"{API}/auth/me", headers={"Authorization": f"Bearer {old_access}"}
                )
                assert whoami.status_code == 401, whoami.text
                refreshed_pair = await client.post(
                    f"{API}/auth/refresh", json={"refresh_token": old_refresh}
                )
                assert refreshed_pair.status_code == 401, refreshed_pair.text
        finally:
            async with maker() as session:
                await session.execute(
                    delete(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
                )
                await session.commit()
            await _cleanup(maker, emails=[email])


# --------------------------------------------------------- email verification


async def test_five_submissions_of_one_verification_code_verify_once(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One 200, four uniform refusals, one transition, one audit entry.

    The barrier is planted on the read of the live challenge rather than
    immediately before the consume, and that placement is forced rather than
    chosen: the service counts an attempt against the row before comparing the
    code, so a barrier after that point would hold a row lock while waiting for
    contenders that cannot reach it. Released here, all five have found the
    same usable challenge and hold the same correct code.

    The losers must be indistinguishable from somebody submitting a wrong code:
    the response says only that the code is invalid, and which condition failed
    stays in the trail.
    """
    suffix = uuid.uuid4().hex[:10]
    email = f"verify-race5-{suffix}@example.com"
    redis = _Redis()
    code, code_hash = generate_verification_code()
    barrier = asyncio.Barrier(SUBMISSIONS)
    original = EmailVerificationRepository.get_active

    async def synchronised_read(
        self: EmailVerificationRepository,
        *,
        user_id: uuid.UUID,
    ) -> EmailVerificationChallenge | None:
        row = await original(self, user_id=user_id)
        await barrier.wait()
        return row

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = await _seed_user(session, email=email, verified=False)
                session.add(
                    EmailVerificationChallenge(
                        user_id=user.id,
                        email=email,
                        code_hash=code_hash,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                )
                await session.commit()
                user_id = user.id

            async with _client(prepared_database, redis) as client:
                signed_in = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": PASSWORD}
                )
                assert signed_in.status_code == 200, signed_in.text
                bearer = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

            monkeypatch.setattr(EmailVerificationRepository, "get_active", synchronised_read)

            async def submit() -> tuple[int, tuple[str, str]]:
                async with _client(prepared_database, redis) as client:
                    response = await client.post(
                        f"{API}/auth/email/verification/verify",
                        json={"code": code},
                        headers=bearer,
                    )
                    if response.status_code == 200:
                        return response.status_code, ("", "")
                    error = response.json()["error"]
                    # `request_id` is per-request by design and is the one field
                    # that must differ; everything a caller could read the
                    # refusal from is compared.
                    return response.status_code, (error["code"], error["message"])

            outcomes = await asyncio.gather(*(submit() for _ in range(SUBMISSIONS)))
            monkeypatch.undo()

            statuses = sorted(status for status, _ in outcomes)
            assert statuses == [200, 422, 422, 422, 422], outcomes

            # Every refusal is the same refusal. A loser that answered
            # differently from a wrong code would say that the code was right
            # and something else went wrong, which is information this endpoint
            # deliberately does not give.
            refusals = {answer for status, answer in outcomes if status != 200}
            assert len(refusals) == 1, refusals

            async with maker() as session:
                challenge = await session.scalar(
                    select(EmailVerificationChallenge).where(
                        EmailVerificationChallenge.user_id == user_id
                    )
                )
                assert challenge is not None
                assert challenge.consumed_at is not None
                verified = await session.get(User, user_id)
                assert verified is not None
                assert verified.email_verified_at is not None
                # One transition recorded, whatever the five requests did.
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.actor_id == user_id,
                            AuditLog.action == AuditAction.EMAIL_VERIFIED,
                        )
                    )
                    == 1
                )
        finally:
            async with maker() as session:
                await session.execute(
                    delete(EmailVerificationChallenge).where(
                        EmailVerificationChallenge.email == email
                    )
                )
                await session.commit()
            await _cleanup(maker, emails=[email])
