"""Committed PostgreSQL races against the last live platform owner.

The guard fixed as AUTHZ-02 reads a set and then acts on it, which is
check-then-act and is worth nothing on its own. With exactly two live owners,
two operations that each remove one both read "two owners, mine may go" and both
commit, and the installation ends up with no platform owner at all - the precise
state the guard exists to make unreachable, arrived at by two operators doing
perfectly ordinary things at the same moment.

So `require_surviving_platform_owner` takes a singleton advisory lock before it
counts, and the count, the decision and the mutation happen in one critical
section. These tests are what says so. None of them uses `db_session`: its outer
transaction is excellent isolation for an ordinary test and cannot represent two
requests that commit independently, and a "race" written inside one transaction
is two sequential calls wearing a costume.

Every race here is *forced* rather than hoped for. A patched `lock_platform_owners`
holds the first contender inside the critical section while the second is
launched and blocks behind the same lock; only then is the first released. That
makes each assertion a statement about serialisation rather than about timing -
the loser's answer is determined by the winner's committed write, which is only
possible if the two were ordered.

Four removal paths can reach zero owners, and all four are raced against each
other: HTTP delete, HTTP disable, the operator CLI's `revoke`, and the demotion
hidden inside `grant <owner> platform_admin`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.core.security import hash_password
from app.db.models import PlatformRole, User
from app.db.models.audit import AuditLog
from app.db.session import Database
from app.main import create_app
from app.platform import hierarchy
from app.platform.hierarchy import live_platform_role
from app.platform.owner_service import PlatformRoleService
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


class _Redis:
    """Enough Redis for login and the limiter, shared by both contenders."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
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
        default_plan_code="",
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


async def _seed_owner(session: AsyncSession, *, email: str) -> User:
    user = User(
        email=email,
        full_name="Platform Owner",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    session.add(user)
    await session.flush()
    return user


async def _bearer(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}


async def _live_owners(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(live_platform_role(PlatformRole.PLATFORM_OWNER))
        )
        return int(total or 0)


async def _cleanup(maker: async_sessionmaker[AsyncSession], *, emails: list[str]) -> None:
    async with maker() as session:
        ids = list((await session.execute(select(User.id).where(User.email.in_(emails)))).scalars())
        if ids:
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(ids)))
            await session.execute(delete(AuditLog).where(AuditLog.target_id.in_(ids)))
        await session.execute(delete(User).where(User.email.in_(emails)))
        await session.commit()


class _Handoff:
    """Forces one contender through the critical section before the other.

    A barrier is the wrong instrument here and trying one is instructive: the
    section is mutually exclusive by design, so the second contender is still
    blocked inside `pg_advisory_xact_lock` while the first waits at the barrier,
    and both time out. The hand-off is explicit instead - the first contender
    signals once it holds the lock and waits there; the second is launched into
    that window and queues behind the same lock; the first is released and
    commits; only then can the second read the owner set.

    Patched onto the module attribute rather than onto the importers, because
    `require_surviving_platform_owner` resolves it from module globals at call
    time - so this covers every caller, including ones added later.
    """

    def __init__(self) -> None:
        self.holds = asyncio.Event()
        self.release = asyncio.Event()
        self._claimed = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = hierarchy.lock_platform_owners

        async def synchronised(session: AsyncSession) -> None:
            await original(session)
            if not self._claimed:
                self._claimed = True
                self.holds.set()
                await asyncio.wait_for(self.release.wait(), timeout=15)

        monkeypatch.setattr(hierarchy, "lock_platform_owners", synchronised)

    async def run(
        self,
        first: Callable[[], Awaitable[str]],
        second: Callable[[], Awaitable[str]],
    ) -> list[str]:
        """Both contenders, ordered, with their outcomes returned as labels."""
        leading = asyncio.create_task(first())
        try:
            await asyncio.wait_for(self.holds.wait(), timeout=15)
        except TimeoutError:  # pragma: no cover - only on a regression
            leading.cancel()
            pytest.fail(
                "the owner guard never took the advisory lock; the count and the "
                "mutation are not one critical section"
            )
        trailing = asyncio.create_task(second())
        # Long enough for the trailing contender to reach the lock it cannot
        # have. Both contenders authenticated before the race began, so nothing
        # in this window can fail for want of a credential - the only thing the
        # delay buys is the guarantee that the trailing request was already
        # queued when the leader committed, and its result is asserted either
        # way.
        await asyncio.sleep(0.5)
        self.release.set()
        return list(await asyncio.gather(leading, trailing))


async def _http_delete(client: AsyncClient, headers: dict[str, str], *, target: uuid.UUID) -> str:
    response = await client.request("DELETE", f"{API}/platform/users/{target}", headers=headers)
    return f"delete:{response.status_code}"


async def _http_disable(client: AsyncClient, headers: dict[str, str], *, target: uuid.UUID) -> str:
    response = await client.post(f"{API}/platform/users/{target}/disable", headers=headers)
    return f"disable:{response.status_code}"


async def _cli_revoke(maker: async_sessionmaker[AsyncSession], *, target: str) -> str:
    """The operator command, at the service it is a thin shell over."""
    async with maker() as session:
        try:
            await PlatformRoleService(session).revoke(target)
        except ValidationError:
            await session.rollback()
            return "revoke:refused"
        await session.commit()
        return "revoke:applied"


async def _cli_demote(maker: async_sessionmaker[AsyncSession], *, target: str) -> str:
    """`grant <owner> platform_admin` - an addition that is really a removal."""
    async with maker() as session:
        try:
            await PlatformRoleService(session).grant(target, PlatformRole.PLATFORM_ADMIN)
        except ValidationError:
            await session.rollback()
            return "demote:refused"
        await session.commit()
        return "demote:applied"


@pytest.mark.parametrize(
    "race",
    [
        "delete x delete",
        "delete x revoke",
        "disable x revoke",
        "revoke x revoke",
        "revoke x demote",
    ],
)
async def test_no_race_between_two_removals_can_empty_the_platform(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    """Two live owners, two concurrent removals, and one must survive.

    Every combination of the four paths that can take platform ownership away is
    run against every other. They are not interchangeable in the code - two are
    HTTP routes through `AccountService`, two are operator commands through
    `PlatformRoleService` - and an invariant held by one pair and not the other
    is exactly the shape of the original finding, where the account lifecycle
    and the role lifecycle each enforced their own idea of what an owner is.

    The assertion is the same in all five cases and it is the only one that
    matters: afterwards, at least one account can still administer this
    platform. The per-race outcomes are asserted too, so a test that reached the
    right count by refusing *both* operations would not pass.
    """
    suffix = uuid.uuid4().hex[:10]
    emails = [f"owner-a-{suffix}@example.com", f"owner-b-{suffix}@example.com"]
    redis = _Redis()
    handoff = _Handoff()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                first_owner = await _seed_owner(session, email=emails[0])
                second_owner = await _seed_owner(session, email=emails[1])
                first_id, second_id = first_owner.id, second_owner.id
                await session.commit()

            assert await _live_owners(maker) == 2

            # Both contenders sign in **before** the race, and the ordering is
            # load-bearing twice over. It is what makes the trailing request one
            # the server has already admitted, so the guard is what refuses it
            # rather than a revoked token - the difference between testing the
            # invariant and testing authentication. And it is what stops the
            # test flaking: a login inside the raced coroutine has to finish
            # within the hand-off window, which it does on an idle machine and
            # does not under a full suite, whereupon the leader's commit has
            # already killed the trailing actor's credential.
            async with (
                _client(prepared_database, redis) as first_client,
                _client(prepared_database, redis) as second_client,
            ):
                first_headers = await _bearer(first_client, emails[0])
                second_headers = await _bearer(second_client, emails[1])

                contenders: dict[str, tuple[Callable[[], Any], Callable[[], Any]]] = {
                    # A and B each remove the other.
                    "delete x delete": (
                        lambda: _http_delete(first_client, first_headers, target=second_id),
                        lambda: _http_delete(second_client, second_headers, target=first_id),
                    ),
                    "delete x revoke": (
                        lambda: _http_delete(first_client, first_headers, target=second_id),
                        lambda: _cli_revoke(maker, target=emails[0]),
                    ),
                    "disable x revoke": (
                        lambda: _http_disable(first_client, first_headers, target=second_id),
                        lambda: _cli_revoke(maker, target=emails[0]),
                    ),
                    "revoke x revoke": (
                        lambda: _cli_revoke(maker, target=emails[0]),
                        lambda: _cli_revoke(maker, target=emails[1]),
                    ),
                    "revoke x demote": (
                        lambda: _cli_revoke(maker, target=emails[0]),
                        lambda: _cli_demote(maker, target=emails[1]),
                    ),
                }
                leading, trailing = contenders[race]
                handoff.install(monkeypatch)
                outcomes = await handoff.run(leading, trailing)

            remaining = await _live_owners(maker)
            assert remaining >= 1, f"{race} emptied the platform: {outcomes}"
            # Exactly one removal may land. Two would be the defect; zero would
            # be a guard that refuses everybody, which would satisfy the count
            # above while breaking the product.
            assert remaining == 1, f"{race} removed nobody: {outcomes}"

            applied = [
                outcome
                for outcome in outcomes
                if outcome.endswith(":applied") or outcome.endswith(":200")
            ]
            refused = [
                outcome
                for outcome in outcomes
                if outcome.endswith(":refused") or outcome.endswith(":422")
            ]
            assert len(applied) == 1, f"{race}: {outcomes}"
            assert len(refused) == 1, f"{race}: {outcomes}"
        finally:
            handoff.release.set()
            await _cleanup(maker, emails=emails)


async def test_a_tombstoned_owner_does_not_satisfy_the_guard(
    prepared_database: str,
) -> None:
    """AUTHZ-02 in its original form, against a committed database.

    Two owners; one is deleted through the platform API; the CLI is then asked
    to revoke the survivor. Before the fix `owners()` selected on `platform_role`
    alone, the tombstone still counted as "remaining", and the command said yes -
    leaving an installation with no platform owner and no supported way back
    except the same command nobody could now authorise.

    No race and no patching: the finding never needed one. This is the order an
    operator does things in.
    """
    suffix = uuid.uuid4().hex[:10]
    emails = [f"ghost-{suffix}@example.com", f"survivor-{suffix}@example.com"]
    redis = _Redis()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                ghost = await _seed_owner(session, email=emails[0])
                survivor = await _seed_owner(session, email=emails[1])
                ghost_id = ghost.id
                await session.commit()
                assert survivor.id  # bound before the session closes

            async with _client(prepared_database, redis) as client:
                headers = await _bearer(client, emails[1])
                deleted = await client.request(
                    "DELETE", f"{API}/platform/users/{ghost_id}", headers=headers
                )
            assert deleted.status_code == 200, deleted.text

            async with maker() as session:
                service = PlatformRoleService(session)
                # The definition itself, asked directly: the tombstone must not
                # appear, whatever its `platform_role` column says.
                owners = await service.owners()
                assert [row.email for row in owners] == [emails[1]]

                with pytest.raises(ValidationError):
                    await service.revoke(emails[1])
                await session.rollback()

            assert await _live_owners(maker) == 1
        finally:
            await _cleanup(maker, emails=emails)
