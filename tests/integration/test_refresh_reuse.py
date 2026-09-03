"""Replaying a spent refresh token, and what it costs the account.

The scenario, stated plainly: somebody's refresh token leaks - a backup, a log,
a browser extension, a shared machine. The thief refreshes. So does the real
person. Rotation alone spends whichever copy arrives; the other chain continues
untouched, and it is usually the thief's, because they are the one watching for
the window.

Detection is therefore the whole game, and it depends on one property: spending
a token must be *atomic*. A denylist read followed by a write is a race that
both parties win, so the version of this feature that checks and then revokes
cannot detect the thing it exists to detect. `RefreshTokenStore.spend` is a
single `SET NX`, and losing that race is the signal.

The response is deliberately heavy: raise `users.token_version`, which
invalidates every access and refresh token the account holds. Both parties are
signed out. The real person signs in again with a password the thief does not
have; the thief has nothing left. Anything gentler leaves the thief holding a
live chain (ADR-039).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import TokenType, decode_token
from app.core.token_store import RefreshTokenStore
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.repositories import MembershipRepository
from app.services.auth_service import AuthenticatedSession, AuthService
from tests.fakes import as_credentials, as_redis_client

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


class _Redis:
    """Enough Redis for the denylist, with `nx` implemented faithfully.

    `nx` returning None without writing when the key exists is the entire
    detection mechanism, so a fake that ignored it would report this file green
    while the real thing was broken.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0


class _RedisClient:
    def __init__(self) -> None:
        self.client = _Redis()


@pytest.fixture
def redis() -> _RedisClient:
    return _RedisClient()


@pytest.fixture
def auth(db_session: AsyncSession, settings: Settings, redis: _RedisClient) -> AuthService:
    return AuthService(
        session=db_session,
        settings=settings,
        token_store=RefreshTokenStore(as_redis_client(redis)),
    )


async def _register(auth: AuthService, session: AsyncSession) -> AuthenticatedSession:
    result = await auth.register(
        email="ahmed@example.test",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await session.flush()
    return result


async def _entries(session: AsyncSession) -> list[AuditLog]:
    rows = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
    return list(rows.scalars().all())


# ------------------------------------------------------------- the happy path


async def test_a_refresh_returns_a_new_pair(auth: AuthService, db_session: AsyncSession) -> None:
    """The control. Every refusal below has to be the reuse check firing, not
    refreshing being broken."""
    session = await _register(auth, db_session)
    assert session is not None

    refreshed = await auth.refresh(refresh_token=session.refresh_token)

    assert refreshed.access_token != session.access_token
    assert refreshed.refresh_token != session.refresh_token


async def test_a_chain_of_refreshes_keeps_working(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """Rotation must not be self-detonating: using each new token in turn is
    exactly what a well-behaved client does."""
    session = await _register(auth, db_session)
    assert session is not None

    token = session.refresh_token
    for _ in range(4):
        token = (await auth.refresh(refresh_token=token)).refresh_token

    assert token


async def test_the_spent_token_is_denylisted_immediately(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    session = await _register(auth, db_session)
    assert session is not None
    claims = decode_token(
        session.refresh_token,
        settings=settings,
        expected_type=TokenType.REFRESH,
    )

    await auth.refresh(refresh_token=session.refresh_token)

    assert await RefreshTokenStore(as_redis_client(redis)).is_revoked(claims.token_id)


# ----------------------------------------------------------------- the replay


async def test_replaying_a_spent_token_is_refused(
    auth: AuthService, db_session: AsyncSession
) -> None:
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)


async def test_replaying_bumps_the_token_version(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The teardown. Without it, detection is a shrug: the presented copy is
    refused and the other chain carries on."""
    session = await _register(auth, db_session)
    assert session is not None
    before = session.user.token_version
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    await db_session.refresh(session.user)
    assert session.user.token_version == before + 1


async def test_the_replay_kills_the_chain_the_thief_took(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The scenario end to end.

    The thief refreshes first and holds a fresh, otherwise-valid pair. The real
    person then presents their copy, which is now spent. That replay must
    invalidate what the thief is holding, not merely refuse the person who was
    robbed.
    """
    session = await _register(auth, db_session)
    assert session is not None
    stolen = await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    # The thief's refresh token was minted under the old version.
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=stolen.refresh_token)


async def test_the_access_token_the_thief_holds_stops_working(
    auth: AuthService, db_session: AsyncSession, settings: Settings
) -> None:
    """Access tokens are not denylisted - they are checked against the version,
    which is what makes a bump reach them (ADR-036)."""
    from app.api.dependencies import get_current_user

    session = await _register(auth, db_session)
    assert session is not None
    stolen = await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)
    await db_session.flush()

    class _Credentials:
        credentials = stolen.access_token

    with pytest.raises(AuthenticationError):
        await get_current_user(
            settings=settings,
            session=db_session,
            credentials=as_credentials(_Credentials()),
        )


async def test_the_real_person_can_sign_in_again_immediately(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The account is not disabled. Only its sessions end, and the password the
    thief does not have is what gets it back."""
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)
    await db_session.flush()

    recovered = await auth.login(email="ahmed@example.test", password=PASSWORD)

    assert recovered.access_token
    # And the new pair works, so the teardown did not leave the account stuck.
    assert await auth.refresh(refresh_token=recovered.refresh_token)


async def test_logging_out_then_refreshing_is_treated_as_a_replay(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """Logout denylists the token, so presenting it afterwards is a spent token
    by the same definition. The teardown is the right response either way: a
    client that refreshes after logging out is either confused or not the
    client."""
    session = await _register(auth, db_session)
    assert session is not None
    before = session.user.token_version
    await auth.logout(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    await db_session.refresh(session.user)
    assert session.user.token_version == before + 1


# ------------------------------------------------------------------ the races


# Both race tests run on their own connections rather than the shared
# transaction fixture. An `AsyncSession` is not safe for concurrent use - two
# coroutines driving one would fail on SQLAlchemy's own state machine long
# before reaching anything under test - and in production each request has its
# own session anyway. So the races are staged the way they actually occur.


async def test_two_simultaneous_presentations_produce_exactly_one_new_pair(
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    """The race that a check-then-write implementation loses silently.

    Both callers read "unspent", both are issued a pair, and the leak is never
    noticed. With an atomic spend exactly one wins and the other is the replay.

    The Redis fake is *shared* between the two callers, which is the point: it
    stands in for the one Redis two application processes both talk to.
    """
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    shared_redis = _RedisClient()
    email, refresh_token = await _seed_account_with(sessions, settings, shared_redis)

    async def present() -> str:
        async with sessions() as session:
            service = AuthService(
                session=session,
                settings=settings,
                token_store=RefreshTokenStore(as_redis_client(shared_redis)),
            )
            try:
                await service.refresh(refresh_token=refresh_token)
                await session.commit()
                return "issued"
            except AuthenticationError:
                return "refused"

    try:
        outcomes = await asyncio.gather(present(), present())

        assert outcomes.count("issued") == 1
        assert outcomes.count("refused") == 1
    finally:
        await _cleanup(sessions, email)


async def test_concurrent_replays_do_not_lose_an_increment(
    engine: AsyncEngine, settings: Settings
) -> None:
    """`token_version += 1` in Python reads a value and writes it back, so two
    teardowns running together would collapse into one increment and leave the
    account on a version an outstanding token still matches.

    The bump is a single `UPDATE ... RETURNING`, so the increments compose.
    """
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    shared_redis = _RedisClient()
    email, refresh_token = await _seed_account_with(sessions, settings, shared_redis)

    async def refresh_once() -> None:
        async with sessions() as session:
            service = AuthService(
                session=session,
                settings=settings,
                token_store=RefreshTokenStore(as_redis_client(shared_redis)),
            )
            await service.refresh(refresh_token=refresh_token)
            await session.commit()

    await refresh_once()

    async def replay() -> None:
        async with sessions() as session:
            service = AuthService(
                session=session,
                settings=settings,
                token_store=RefreshTokenStore(as_redis_client(shared_redis)),
            )
            with pytest.raises(AuthenticationError):
                await service.refresh(refresh_token=refresh_token)

    try:
        await asyncio.gather(replay(), replay(), replay())

        async with sessions() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            # One increment per replay. Started at 1, so three replays reach 4.
            assert user.token_version == 4
    finally:
        await _cleanup(sessions, email)


async def _seed_account_with(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: _RedisClient,
) -> tuple[str, str]:
    email = f"race-{uuid.uuid4().hex[:10]}@example.test"
    async with sessions() as session:
        service = AuthService(
            session=session,
            settings=settings,
            token_store=RefreshTokenStore(as_redis_client(redis)),
        )
        result = await service.register(
            email=email,
            password=PASSWORD,
            workspace_name="Race",
            workspace_slug=f"race-{uuid.uuid4().hex[:8]}",
        )
        await session.commit()
        return email, result.refresh_token


async def _cleanup(sessions: async_sessionmaker[AsyncSession], email: str) -> None:
    """Remove what the race tests committed.

    They cannot use the rolled-back fixture transaction, so they tidy up after
    themselves - otherwise the rows outlive the test and the next run trips the
    unique constraint on the address.
    """
    async with sessions() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            return
        memberships = (
            (await session.execute(select(Membership).where(Membership.user_id == user.id)))
            .scalars()
            .all()
        )
        tenant_ids = [membership.tenant_id for membership in memberships]
        await session.execute(
            delete(AuditLog).where(AuditLog.actor_id == user.id),
        )
        await session.execute(delete(Membership).where(Membership.user_id == user.id))
        await session.delete(user)
        if tenant_ids:
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        await session.commit()


# ----------------------------------------------------------- what it records


async def test_the_replay_is_recorded_in_the_audit_trail(
    auth: AuthService, db_session: AsyncSession
) -> None:
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    entries = [
        entry
        for entry in await _entries(db_session)
        if entry.action is AuditAction.REFRESH_TOKEN_REUSED
    ]
    assert len(entries) == 1
    entry = entries[0]
    # A system observation, not something the person did.
    assert entry.actor_kind is AuditActorKind.SYSTEM
    assert entry.target_type == "user"
    assert entry.target_id == session.user.id
    assert entry.target_label == "ahmed@example.test"
    assert entry.meta is not None
    assert entry.meta["token_version"] == session.user.token_version
    # Platform-level: an account is a global identity, so this is not one
    # workspace's business.
    assert entry.tenant_id is None


async def test_the_entry_survives_the_failed_request(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The teardown commits before the refusal is raised.

    A revocation staged the ordinary way would be rolled back by the very
    exception that accompanies it, which would make the audit entry a record of
    something that did not happen and leave the estate live.
    """
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)
    # Roll back the way a failing request would.
    await db_session.rollback()

    survivors = [
        entry
        for entry in await _entries(db_session)
        if entry.action is AuditAction.REFRESH_TOKEN_REUSED
    ]
    assert len(survivors) == 1


async def test_no_token_material_reaches_the_audit_trail(
    auth: AuthService, db_session: AsyncSession
) -> None:
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    for entry in await _entries(db_session):
        rendered = f"{entry.target_label} {entry.actor_label} {entry.meta}"
        assert session.refresh_token not in rendered
        assert session.access_token not in rendered


async def test_no_token_material_reaches_the_log(
    auth: AuthService, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with caplog.at_level(logging.DEBUG), pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    text = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert session.refresh_token not in text
    assert session.access_token not in text
    # The event itself is logged, loudly - it is the thing somebody should be
    # woken for.
    assert "auth.refresh_token_reused" in text


async def test_the_refusal_says_nothing_about_what_happened(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """A caller replaying a token learns only that it did not work. Telling
    them the estate was just torn down tells a thief to move faster."""
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)

    with pytest.raises(AuthenticationError) as raised:
        await auth.refresh(refresh_token=session.refresh_token)

    message = str(raised.value).lower()
    for leak in ("reuse", "replay", "revoked", "version", "denylist", "detected"):
        assert leak not in message


# ------------------------------------------------------- the odd cases


async def test_a_replay_for_a_deleted_account_is_refused_without_a_trail(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The token was signed by us, so this is a deleted user rather than a
    forgery. There is nothing left to revoke and nothing to record it against."""
    session = await _register(auth, db_session)
    assert session is not None
    await auth.refresh(refresh_token=session.refresh_token)
    assert session.workspace is not None
    memberships = MembershipRepository(
        db_session,
        tenant_id=session.workspace.tenant.id,
    )
    await memberships.get_for_user(session.user.id)
    await db_session.delete(session.user)
    await db_session.flush()

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)

    entries = [
        entry
        for entry in await _entries(db_session)
        if entry.action is AuditAction.REFRESH_TOKEN_REUSED
    ]
    assert entries == []


async def test_a_forged_token_never_reaches_the_teardown(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """Signature verification happens first, so an unsigned string cannot be
    used to raise somebody else's token version - which would otherwise be a
    denial-of-service against any account whose id an attacker could guess."""
    session = await _register(auth, db_session)
    assert session is not None
    before = session.user.token_version

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token="not.a.token")

    await db_session.refresh(session.user)
    assert session.user.token_version == before


async def test_an_access_token_cannot_be_presented_as_a_refresh_token(
    auth: AuthService, db_session: AsyncSession
) -> None:
    session = await _register(auth, db_session)
    assert session is not None

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.access_token)


async def test_a_workspace_owner_keeps_their_workspace_after_a_teardown(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """The teardown ends sessions. It must not touch membership - being robbed
    is not a reason to lose a company."""
    session = await _register(auth, db_session)
    assert session is not None
    assert session.workspace is not None
    tenant = session.workspace.tenant
    await auth.refresh(refresh_token=session.refresh_token)
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token)
    await db_session.flush()

    memberships = MembershipRepository(db_session, tenant_id=tenant.id)
    still_theirs = await memberships.get_for_user(session.user.id)

    assert still_theirs is not None
    assert still_theirs.role is TenantRole.TENANT_OWNER
    assert isinstance(tenant, Tenant)
    assert tenant.status is TenantStatus.ACTIVE
    assert isinstance(session.user, User)
