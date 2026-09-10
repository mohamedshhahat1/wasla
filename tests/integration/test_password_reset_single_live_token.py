"""One live password reset token per account, kept by PostgreSQL.

`email_verification_challenges` has enforced its version of this rule with a
partial unique index since it was created. `password_reset_tokens` did not: the
invariant rested entirely on `supersede_outstanding` being called before
`create`, so a future caller that forgot would leave two simultaneously valid
reset links and nothing would say so (AUTH-09). Two sibling tables enforcing one
rule at two strengths, and the weaker one guarding a password.

`uq_password_reset_tokens_active` closes that. What the tests below separate is
the two halves of the change, because they fail for different reasons and a
regression in either is a different bug:

* **the index** refuses a second live row for one account, whatever wrote it -
  including the repository directly, which is the case a service-level test
  could never reach;
* **the advisory lock** in `PasswordResetService.request` means nothing in the
  product ever meets that refusal. Two simultaneous requests are serialised per
  account, so the second supersedes the first's token rather than colliding
  with it, and the endpoint keeps answering its one constant answer.

The second is the part worth being careful about. An invariant enforced by a
constraint the application walks into is an invariant that turns an endpoint
whose entire contract is "say nothing either way" into a 500 - which would be a
new account-existence oracle in the name of closing a database finding.

The concurrency tests commit independently over their own connections, so
nothing here uses `db_session`; rows are uniquely named and removed in a
`finally`.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import generate_reset_token, hash_password
from app.db.models import User
from app.db.models.email import OutboundEmail
from app.db.models.password_reset import PasswordResetToken
from app.db.session import Database
from app.main import create_app
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
INDEX_NAME = "uq_password_reset_tokens_active"

# Four simultaneous requests for one address. More than two, because the
# failure this guards against is not "the second one loses" - it is any pair of
# them both inserting, and a wider field makes an accidental serialisation a
# less plausible explanation for a pass.
REQUESTS = 4


class _Redis:
    async def check(self, timeout_seconds: float | None = None) -> None:
        return None

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        return True

    async def exists(self, key: str) -> int:
        return 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

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
async def _client(database_url: str) -> AsyncIterator[AsyncClient]:
    settings = _settings(database_url)
    application = create_app(settings)
    database = Database(settings)
    application.state.database = database
    application.state.redis = _Redis()
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


async def _seed(maker: async_sessionmaker[AsyncSession], *, email: str) -> uuid.UUID:
    async with maker() as session:
        user = User(
            email=email,
            hashed_password=hash_password(PASSWORD),
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()
        return user.id


async def _live(maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.consumed_at.is_(None),
                    PasswordResetToken.superseded_at.is_(None),
                )
            )
            or 0
        )


async def _cleanup(maker: async_sessionmaker[AsyncSession], *, email: str) -> None:
    async with maker() as session:
        await session.execute(delete(OutboundEmail).where(OutboundEmail.recipient == email))
        user_ids = list(
            (await session.execute(select(User.id).where(User.email == email))).scalars()
        )
        if user_ids:
            await session.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(user_ids))
            )
        await session.execute(delete(User).where(User.email == email))
        await session.commit()


# ------------------------------------------------------------- the index


async def test_the_database_refuses_a_second_live_token(db_session: AsyncSession) -> None:
    """Written straight through the repository, past every service guard.

    This is the case the finding is actually about: not that the service
    misbehaves, but that nothing except the service's own memory stopped a
    second live reset link from existing. A caller that skips
    `supersede_outstanding` must now fail loudly rather than quietly hand out
    two working links.
    """
    user = User(
        email=f"two-live-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    tokens = PasswordResetTokenRepository(db_session)
    expires = datetime.now(UTC) + timedelta(minutes=30)
    _, first_hash = generate_reset_token()
    _, second_hash = generate_reset_token()

    await tokens.create(user_id=user.id, token_hash=first_hash, expires_at=expires)

    with pytest.raises(IntegrityError) as refused:
        await tokens.create(user_id=user.id, token_hash=second_hash, expires_at=expires)

    # Named, so this cannot be satisfied by some unrelated constraint - the
    # unique index on `token_hash`, say, which two different tokens would not
    # trip anyway.
    assert INDEX_NAME in str(refused.value)


@pytest.mark.parametrize("ending", ["consumed", "superseded"])
async def test_a_token_that_is_no_longer_live_does_not_block_a_new_one(
    db_session: AsyncSession,
    ending: str,
) -> None:
    """The predicate has to let the ordinary flow work.

    An index that refused a new token because the account once had one would
    lock people out of their own recovery path after a single reset - which is
    a worse failure than the one being fixed, and is exactly what a predicate
    written as `unique(user_id)` would do.
    """
    user = User(
        email=f"reissue-{uuid.uuid4().hex[:10]}@example.com",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    tokens = PasswordResetTokenRepository(db_session)
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=30)
    _, first_hash = generate_reset_token()
    _, second_hash = generate_reset_token()

    first = await tokens.create(user_id=user.id, token_hash=first_hash, expires_at=expires)
    if ending == "consumed":
        assert await tokens.consume(token_id=first.id, now=now)
    else:
        assert await tokens.supersede_outstanding(user_id=user.id, now=now) == 1
    await db_session.flush()

    second = await tokens.create(user_id=user.id, token_hash=second_hash, expires_at=expires)
    assert second.id != first.id

    # Both rows are still there. Superseding is not deleting: the row records
    # that a link was issued and then invalidated, which is worth keeping.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id)
        )
        == 2
    )


# --------------------------------------------------------------- the lock


async def test_repeated_requests_leave_exactly_one_live_token(
    prepared_database: str,
) -> None:
    """The ordinary flow, sequentially: ask three times, hold one live link."""
    email = f"reset-seq-{uuid.uuid4().hex[:10]}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            user_id = await _seed(maker, email=email)

            async with _client(prepared_database) as client:
                for _ in range(3):
                    response = await client.post(
                        f"{API}/auth/password-reset/request", json={"email": email}
                    )
                    assert response.status_code == 202, response.text

            assert await _live(maker, user_id) == 1
            async with maker() as session:
                total = await session.scalar(
                    select(func.count())
                    .select_from(PasswordResetToken)
                    .where(PasswordResetToken.user_id == user_id)
                )
            # Three asked for, two superseded, one live.
            assert total == 3
        finally:
            await _cleanup(maker, email=email)


async def test_simultaneous_requests_leave_one_live_token_and_no_500(
    prepared_database: str,
) -> None:
    """The race the new index would otherwise turn into an error.

    Four requests for the same address at once. Without the advisory lock they
    all supersede nothing, all insert, and the index refuses three of them -
    correct about the invariant, and a 500 from an endpoint whose entire
    contract is one constant answer whether or not the address is registered.
    A 500 here would be a fresh account-existence oracle introduced in the act
    of closing a database finding.

    So: four 202s, one live token, and the invariant held by PostgreSQL rather
    than by anybody's care.
    """
    email = f"reset-race-{uuid.uuid4().hex[:10]}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            user_id = await _seed(maker, email=email)

            async def request() -> int:
                async with _client(prepared_database) as client:
                    response = await client.post(
                        f"{API}/auth/password-reset/request", json={"email": email}
                    )
                    return response.status_code

            statuses = await asyncio.gather(*(request() for _ in range(REQUESTS)))

            assert statuses == [202] * REQUESTS, statuses
            assert await _live(maker, user_id) == 1

            async with maker() as session:
                total = await session.scalar(
                    select(func.count())
                    .select_from(PasswordResetToken)
                    .where(PasswordResetToken.user_id == user_id)
                )
            # Every request that answered 202 really did issue a token; the
            # losers' tokens are superseded rather than absent, so the count is
            # a check that no request quietly did nothing.
            assert total == REQUESTS
        finally:
            await _cleanup(maker, email=email)


async def test_the_live_token_after_a_race_is_the_one_that_works(
    prepared_database: str,
) -> None:
    """One survivor, and it is a real one.

    "Exactly one live row" would be satisfied by a row nothing can redeem, so
    this follows the surviving token through to a completed reset: the password
    changes, and the account's other tokens are dead.
    """
    email = f"reset-race-usable-{uuid.uuid4().hex[:10]}@example.com"
    new_password = "a replacement passphrase entirely"

    async with _sessions(prepared_database) as maker:
        try:
            user_id = await _seed(maker, email=email)

            async def request() -> int:
                async with _client(prepared_database) as client:
                    response = await client.post(
                        f"{API}/auth/password-reset/request", json={"email": email}
                    )
                    return response.status_code

            assert await asyncio.gather(*(request() for _ in range(REQUESTS))) == [202] * REQUESTS

            # The raw token exists only in the outbox row, which is where the
            # real recipient would read it from.
            async with maker() as session:
                live = (
                    await session.scalars(
                        select(PasswordResetToken).where(
                            PasswordResetToken.user_id == user_id,
                            PasswordResetToken.consumed_at.is_(None),
                            PasswordResetToken.superseded_at.is_(None),
                        )
                    )
                ).all()
            assert len(live) == 1

            from app.services.email_service import open_email_context

            async with maker() as session:
                queued = (
                    await session.scalars(
                        select(OutboundEmail).where(
                            OutboundEmail.idempotency_key == f"password-reset:{live[0].id}"
                        )
                    )
                ).all()
            assert len(queued) == 1
            raw_token = open_email_context(queued[0], _settings(prepared_database))["token"]

            async with _client(prepared_database) as client:
                confirmed = await client.post(
                    f"{API}/auth/password-reset/confirm",
                    json={"token": raw_token, "new_password": new_password},
                )
                assert confirmed.status_code == 200, confirmed.text

                signed_in = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": new_password}
                )
                assert signed_in.status_code == 200, signed_in.text

            # Nothing live is left, so the account is not carrying a spare way
            # in behind the reset that just happened.
            assert await _live(maker, user_id) == 0
        finally:
            await _cleanup(maker, email=email)
