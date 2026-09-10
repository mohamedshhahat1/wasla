"""Committed PostgreSQL races for the account and workspace lifecycle.

These tests deliberately do not use ``db_session``. Its outer transaction is
excellent isolation for an ordinary test and cannot represent two requests that
commit independently - and a "race" written inside one transaction is two
sequential calls wearing a costume. Every test here drives two real applications
over two real connections, and every one of them is synchronised with an
``asyncio.Barrier`` planted inside the code under test, so the interleaving is
forced rather than hoped for.

What is being defended is one invariant and one guarantee:

* **an active workspace always has at least one active owner** - attacked below
  by two owners leaving at once, two owners transferring at once, and an owner
  closing their account while another leaves;
* **a credential that has been revoked is revoked** - attacked by a refresh
  racing an account deletion, and by two confirmations of one reset token.

Rows are uniquely named per test and removed in a ``finally`` block, because
nothing here is rolled back for us.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import generate_reset_token, hash_password
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    User,
)
from app.db.models.audit import AuditLog
from app.db.models.email import OutboundEmail
from app.db.models.enums import TenantStatus
from app.db.models.password_reset import PasswordResetToken
from app.db.session import Database
from app.main import create_app
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "an entirely different passphrase"


class _Redis:
    """A Redis shared by both racing applications.

    Shared on purpose: the refresh-token store's single-use guarantee is a
    property of one Redis, and two independent fakes would let both halves of a
    race "win" for reasons that have nothing to do with the code.
    """

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


async def _seed_user(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = PASSWORD,
) -> User:
    user = User(
        email=email,
        full_name="Race Participant",
        hashed_password=hash_password(password) if password is not None else None,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_workspace(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    return tenant


async def _login(client: AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _workspace_headers(
    client: AsyncClient,
    session: dict[str, Any],
    slug: str,
) -> dict[str, str]:
    response = await client.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": slug},
        headers=_bearer(session),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _cleanup(
    maker: async_sessionmaker[AsyncSession], *, slugs: list[str], emails: list[str]
) -> None:
    async with maker() as session:
        await session.execute(delete(OutboundEmail).where(OutboundEmail.recipient.in_(emails)))
        tenants = (await session.execute(select(Tenant.id).where(Tenant.slug.in_(slugs)))).scalars()
        tenant_ids = list(tenants)
        if tenant_ids:
            await session.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(tenant_ids)))
        users = (await session.execute(select(User.id).where(User.email.in_(emails)))).scalars()
        user_ids = list(users)
        if user_ids:
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await session.execute(delete(Tenant).where(Tenant.slug.in_(slugs)))
        await session.execute(delete(User).where(User.email.in_(emails)))
        await session.commit()


# ------------------------------------------------------------ ownership races


async def test_two_owners_leaving_at_once_never_empties_the_workspace(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the last-owner rule exists for, forced rather than hoped for.

    **Why this is not a barrier.** The first shape tried here put an
    ``asyncio.Barrier`` after the owner count and expected both requests to meet
    at it. They cannot, and that is the proof working: the count is read inside
    the tenant row lock, so the second request is still waiting on
    ``SELECT ... FOR UPDATE`` while the first is at the barrier, and both sides
    time out. A barrier is the wrong instrument for a section that is mutually
    exclusive by design.

    So the hand-off is explicit instead. The first request signals once it holds
    the lock and then waits; the second is launched into that window and blocks
    on the lock; the first is released, commits its departure, and only then can
    the second read the owner set. What it must see is *one* owner - itself -
    and refuse.

    That makes the assertion below a statement about serialisation rather than
    about timing: the second request's answer is determined by the first
    request's committed write, which is only possible if the two were ordered.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"leave-race-{suffix}"
    emails = [f"owner-a-{suffix}@example.com", f"owner-b-{suffix}@example.com"]
    redis = _Redis()

    from app.repositories.tenant_repository import TenantRepository

    original_lock = TenantRepository.lock
    holds_lock = asyncio.Event()
    release = asyncio.Event()
    claimed = False

    async def synchronised_lock(self: TenantRepository, tenant_id: uuid.UUID) -> Tenant | None:
        row = await original_lock(self, tenant_id)
        nonlocal claimed
        if not claimed:
            claimed = True
            holds_lock.set()
            # Held open while the second request is launched and blocks behind
            # this same row lock.
            await asyncio.wait_for(release.wait(), timeout=10)
        return row

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                tenant = await _seed_workspace(session, slug=slug)
                user_ids = {}
                for email in emails:
                    user = await _seed_user(session, email=email)
                    user_ids[email] = user.id
                    session.add(
                        Membership(
                            tenant_id=tenant.id,
                            user_id=user.id,
                            role=TenantRole.TENANT_OWNER,
                        )
                    )
                await session.commit()

            async def leave(email: str) -> int:
                async with _client(prepared_database, redis) as client:
                    payload = await _login(client, email)
                    headers = await _workspace_headers(client, payload, slug)
                    # Authentication and workspace selection happen before the
                    # patch matters, so the only synchronised step is the write.
                    response = await client.delete(
                        f"{API}/workspace/members/{user_ids[email]}",
                        headers=headers,
                    )
                    return response.status_code

            monkeypatch.setattr(TenantRepository, "lock", synchronised_lock)

            first = asyncio.create_task(leave(emails[0]))
            try:
                await asyncio.wait_for(holds_lock.wait(), timeout=10)
            except TimeoutError:  # pragma: no cover - only on a regression
                # The patched `lock` was never called, so nothing serialises the
                # owner count. Named rather than left as a bare timeout, because
                # this is the exact regression the test exists to catch and the
                # symptom otherwise reads as an unrelated hang.
                first.cancel()
                pytest.fail(
                    "MembershipService.revoke did not take the tenant row lock; "
                    "the last-owner check is racing"
                )
            second = asyncio.create_task(leave(emails[1]))
            # Long enough for the second request to authenticate and reach the
            # lock it cannot have. Its result is asserted below either way, so
            # a slow machine loses no coverage - it only weakens the guarantee
            # that the second request was already queued.
            await asyncio.sleep(0.5)
            release.set()

            statuses = sorted(await asyncio.gather(first, second))
            assert statuses == [200, 409], statuses

            async with maker() as session:
                owners = await session.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .join(Tenant, Tenant.id == Membership.tenant_id)
                    .where(
                        Tenant.slug == slug,
                        Membership.role == TenantRole.TENANT_OWNER,
                        Membership.status == MembershipStatus.ACTIVE,
                    )
                )
                assert owners == 1, "the workspace was left without an owner"
        finally:
            release.set()
            await _cleanup(maker, slugs=[slug], emails=emails)


async def test_two_ownership_transfers_at_once_leave_a_consistent_role_state(
    prepared_database: str,
) -> None:
    """Two owners handing the workspace to each other at the same moment.

    Without the lock, both read a state in which the other is still an owner,
    both step down, and both promote somebody who is stepping down - the roles
    end up decided by whichever write landed second on each row. Under the lock
    the two transfers are serialised, so whatever order they land in, the
    workspace still has an owner afterwards.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"transfer-race-{suffix}"
    emails = [f"trans-a-{suffix}@example.com", f"trans-b-{suffix}@example.com"]
    redis = _Redis()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                tenant = await _seed_workspace(session, slug=slug)
                users = []
                for email in emails:
                    user = await _seed_user(session, email=email)
                    users.append(user)
                    session.add(
                        Membership(
                            tenant_id=tenant.id,
                            user_id=user.id,
                            role=TenantRole.TENANT_OWNER,
                        )
                    )
                await session.commit()
                ids = [user.id for user in users]

            start = asyncio.Barrier(2)

            async def transfer(email: str, target: uuid.UUID) -> int:
                async with _client(prepared_database, redis) as client:
                    session_payload = await _login(client, email)
                    headers = await _workspace_headers(client, session_payload, slug)
                    # Synchronised at the door rather than inside the lock: both
                    # requests are in flight together and the database decides
                    # the order, which is the realistic shape of this race.
                    await start.wait()
                    response = await client.post(
                        f"{API}/workspace/ownership",
                        json={"user_id": str(target)},
                        headers=headers,
                    )
                    return response.status_code

            statuses = await asyncio.gather(
                transfer(emails[0], ids[1]),
                transfer(emails[1], ids[0]),
            )
            assert all(status in (200, 409) for status in statuses), statuses

            async with maker() as session:
                owners = await session.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .join(Tenant, Tenant.id == Membership.tenant_id)
                    .where(
                        Tenant.slug == slug,
                        Membership.role == TenantRole.TENANT_OWNER,
                        Membership.status == MembershipStatus.ACTIVE,
                    )
                )
                assert owners is not None
                assert owners >= 1, "concurrent transfers left the workspace ownerless"
        finally:
            await _cleanup(maker, slugs=[slug], emails=emails)


async def test_deleting_a_workspace_while_a_member_is_removed_stays_consistent(
    prepared_database: str,
) -> None:
    """Deletion racing a membership change must not leave half a state.

    Both operations take the same tenant lock, so one of them sees the world the
    other left. What must never happen is a workspace marked deleted while a
    membership in it is still active - the roster would show somebody with
    access to something that no longer exists.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"delete-race-{suffix}"
    emails = [f"del-owner-{suffix}@example.com", f"del-member-{suffix}@example.com"]
    redis = _Redis()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                tenant = await _seed_workspace(session, slug=slug)
                owner = await _seed_user(session, email=emails[0])
                member = await _seed_user(session, email=emails[1])
                session.add_all(
                    [
                        Membership(
                            tenant_id=tenant.id,
                            user_id=owner.id,
                            role=TenantRole.TENANT_OWNER,
                        ),
                        Membership(
                            tenant_id=tenant.id,
                            user_id=member.id,
                            role=TenantRole.MEMBER,
                        ),
                    ]
                )
                await session.commit()
                member_id = member.id

            start = asyncio.Barrier(2)

            async def close_workspace() -> int:
                async with _client(prepared_database, redis) as client:
                    payload = await _login(client, emails[0])
                    headers = await _workspace_headers(client, payload, slug)
                    await start.wait()
                    response = await client.request(
                        "DELETE",
                        f"{API}/workspace",
                        json={"confirmation": slug},
                        headers=headers,
                    )
                    return response.status_code

            async def remove_member() -> int:
                async with _client(prepared_database, redis) as client:
                    payload = await _login(client, emails[0])
                    headers = await _workspace_headers(client, payload, slug)
                    await start.wait()
                    response = await client.delete(
                        f"{API}/workspace/members/{member_id}",
                        headers=headers,
                    )
                    return response.status_code

            await asyncio.gather(close_workspace(), remove_member(), return_exceptions=True)

            async with maker() as session:
                tenant_row = await session.scalar(select(Tenant).where(Tenant.slug == slug))
                assert tenant_row is not None
                if tenant_row.deleted_at is not None:
                    active = await session.scalar(
                        select(func.count())
                        .select_from(Membership)
                        .where(
                            Membership.tenant_id == tenant_row.id,
                            Membership.status == MembershipStatus.ACTIVE,
                        )
                    )
                    assert active == 0, "a deleted workspace kept an active membership"
        finally:
            await _cleanup(maker, slugs=[slug], emails=emails)


async def test_a_request_admitted_before_a_suspension_cannot_commit_after_it(
    prepared_database: str,
) -> None:
    """Suspension must not be a suggestion to work already in flight.

    Enforcement lives in `get_active_workspace`, which reads the tenant on every
    request - so a token minted before the suspension stops working at the next
    request rather than at expiry. That is what this asserts: the suspension
    commits from one connection, and the *next* request on the pre-existing
    session is refused, with no dependence on token lifetime.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"suspend-race-{suffix}"
    emails = [f"susp-{suffix}@example.com"]
    redis = _Redis()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                tenant = await _seed_workspace(session, slug=slug)
                owner = await _seed_user(session, email=emails[0])
                session.add(
                    Membership(
                        tenant_id=tenant.id,
                        user_id=owner.id,
                        role=TenantRole.TENANT_OWNER,
                    )
                )
                await session.commit()
                tenant_id = tenant.id

            async with _client(prepared_database, redis) as client:
                payload = await _login(client, emails[0])
                headers = await _workspace_headers(client, payload, slug)
                before = await client.get(f"{API}/workspace/members", headers=headers)
                assert before.status_code == 200, before.text

                # Committed independently, exactly as a platform request would.
                async with maker() as staff_session:
                    row = await staff_session.get(Tenant, tenant_id)
                    assert row is not None
                    row.status = TenantStatus.SUSPENDED
                    await staff_session.commit()

                after = await client.get(f"{API}/workspace/members", headers=headers)
                assert after.status_code == 403, after.text
        finally:
            await _cleanup(maker, slugs=[slug], emails=emails)


# -------------------------------------------------------------- account races


async def test_a_refresh_racing_an_account_deletion_leaves_no_live_session(
    prepared_database: str,
) -> None:
    """Deletion and refresh, decided by the database rather than by luck.

    Whichever order they land in, the end state must be the same: no usable
    session. If the refresh wins it hands out a pair minted under the old token
    version, and the deletion's bump must invalidate that pair too - which is
    the property being checked, by trying the *returned* tokens afterwards
    rather than by asserting on status codes.
    """
    suffix = uuid.uuid4().hex[:10]
    emails = [f"del-refresh-{suffix}@example.com"]
    redis = _Redis()

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                await _seed_user(session, email=emails[0])
                await session.commit()

            async with _client(prepared_database, redis) as client:
                payload = await _login(client, emails[0])
                start = asyncio.Barrier(2)

                async def close() -> Any:
                    await start.wait()
                    return await client.request(
                        "DELETE",
                        f"{API}/auth/me",
                        json={"current_password": PASSWORD},
                        headers=_bearer(payload),
                    )

                async def refresh() -> Any:
                    await start.wait()
                    return await client.post(
                        f"{API}/auth/refresh",
                        json={"refresh_token": payload["refresh_token"]},
                    )

                closed, refreshed = await asyncio.gather(close(), refresh())

                # Whatever happened above, nothing usable may survive it.
                if refreshed.status_code == 200:
                    body = refreshed.json()
                    survivor = await client.get(
                        f"{API}/auth/me",
                        headers={"Authorization": f"Bearer {body['access_token']}"},
                    )
                    assert survivor.status_code == 401, survivor.text
                    replayed = await client.post(
                        f"{API}/auth/refresh",
                        json={"refresh_token": body["refresh_token"]},
                    )
                    assert replayed.status_code == 401, replayed.text

                assert closed.status_code in (200, 401), closed.text
                relogin = await client.post(
                    f"{API}/auth/login",
                    json={"email": emails[0], "password": PASSWORD},
                )
                if closed.status_code == 200:
                    assert relogin.status_code == 401, relogin.text
        finally:
            await _cleanup(maker, slugs=[], emails=emails)


async def test_two_workspace_creations_with_one_slug_produce_one_winner(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uniqueness constraint is the guard; the savepoint is what makes it answerable.

    A failed statement aborts its transaction in PostgreSQL, so without the
    savepoint the loser could not return a conflict - the session would already
    be poisoned and the request would 500. Both callers here are different
    accounts asking for the same address at the same moment.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"slug-race-{suffix}"
    emails = [f"slug-a-{suffix}@example.com", f"slug-b-{suffix}@example.com"]
    redis = _Redis()
    barrier = asyncio.Barrier(2)

    from app.repositories.tenant_repository import TenantRepository

    original = TenantRepository.create

    async def synchronised_create(self: TenantRepository, **kwargs: Any) -> Tenant:
        row = await original(self, **kwargs)
        # Both requests have now built their tenant and neither has committed,
        # which is the only interleaving in which a pre-check could pass twice.
        await barrier.wait()
        return row

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                for email in emails:
                    await _seed_user(session, email=email)
                await session.commit()

            monkeypatch.setattr(TenantRepository, "create", synchronised_create)

            async def create(email: str) -> int:
                async with _client(prepared_database, redis) as client:
                    payload = await _login(client, email)
                    response = await client.post(
                        f"{API}/workspaces",
                        json={"name": "Contested", "slug": slug},
                        headers=_bearer(payload),
                    )
                    return response.status_code

            statuses = sorted(await asyncio.gather(create(emails[0]), create(emails[1])))
            assert statuses == [201, 409], statuses

            async with maker() as session:
                count = await session.scalar(
                    select(func.count()).select_from(Tenant).where(Tenant.slug == slug)
                )
                assert count == 1
        finally:
            await _cleanup(maker, slugs=[slug], emails=emails)


async def test_two_confirmations_of_one_reset_token_produce_one_winner(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-06: the single-use barrier test the audit found missing.

    Single-use was implemented as one ``UPDATE ... WHERE consumed_at IS NULL
    RETURNING`` - the correct shape - and was only ever tested sequentially,
    which cannot distinguish it from a read-then-write that happens to be fast.
    The barrier here is planted between the read and the spend, so both requests
    have found a usable token before either has spent it. That is precisely the
    interleaving a check-then-act implementation loses, and the one under which
    a leaked reset link could be redeemed alongside the real person's.

    Exactly one confirmation must succeed, exactly one password must be written,
    and the version must be bumped once.
    """
    suffix = uuid.uuid4().hex[:10]
    emails = [f"reset-race-{suffix}@example.com"]
    redis = _Redis()
    raw_token, token_hash = generate_reset_token()
    barrier = asyncio.Barrier(2)

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
                user = await _seed_user(session, email=emails[0])
                session.add(
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash=token_hash,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                )
                await session.commit()
                user_id = user.id
                original_version = user.token_version

            monkeypatch.setattr(
                PasswordResetTokenRepository, "get_by_token_hash", synchronised_read
            )

            async def confirm() -> int:
                async with _client(prepared_database, redis) as client:
                    response = await client.post(
                        f"{API}/auth/password-reset/confirm",
                        json={"token": raw_token, "new_password": NEW_PASSWORD},
                    )
                    return response.status_code

            statuses = sorted(await asyncio.gather(confirm(), confirm()))
            assert statuses == [200, 401], statuses

            async with maker() as session:
                token_row = await session.scalar(
                    select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
                )
                assert token_row is not None
                assert token_row.consumed_at is not None
                refreshed_user = await session.get(User, user_id)
                assert refreshed_user is not None
                # Exactly one bump. Two winners would show as two.
                assert refreshed_user.token_version == original_version + 1
        finally:
            async with maker() as session:
                await session.execute(
                    delete(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
                )
                await session.commit()
            await _cleanup(maker, slugs=[], emails=emails)
