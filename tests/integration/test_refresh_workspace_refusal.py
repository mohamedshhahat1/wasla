"""A refresh a workspace refuses does not spend the token (FEF-24, ADR-197).

A person reloads a page of a workspace staff suspended. The refresh into it is
refused, 403 `workspace_suspended` - and that refusal used to come *after* the
token was spent, so the browser kept a spent token: its next "Check again" was
a replay, which tears every session of the account down (ADR-039). Now the
workspace is asked before the token is spent, so asking again after a restore
is an ordinary refresh, and asking again while still suspended is the same
refusal. Reuse detection is unchanged: a token that *was* spent is a replay
whatever the workspace says.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError, TenantIsolationError
from app.core.security import TokenType, decode_token
from app.core.token_store import RefreshTokenStore
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.enums import MembershipStatus
from app.services.auth_service import AuthenticatedSession, AuthService
from tests.fakes import as_redis_client
from tests.integration.test_refresh_reuse import PASSWORD, _RedisClient
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

SLUG = "acme-refusal"


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
        email="refusal@example.test",
        password=PASSWORD,
        timezone=TEST_TIMEZONE,
        workspace_name="Acme",
        workspace_slug=SLUG,
    )
    await session.flush()
    return result


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = (await session.execute(select(Tenant).where(Tenant.slug == SLUG))).scalar_one()
    return tenant


async def _set_status(session: AsyncSession, status: TenantStatus) -> None:
    (await _tenant(session)).status = status
    await session.flush()


async def _spent(redis: _RedisClient, token: str, settings: Settings) -> bool:
    claims = decode_token(token, settings=settings, expected_type=TokenType.REFRESH)
    return await RefreshTokenStore(as_redis_client(redis)).is_revoked(claims.token_id)


async def _version(session: AsyncSession) -> int:
    user = (
        await session.execute(select(User).where(User.email == "refusal@example.test"))
    ).scalar_one()
    await session.refresh(user)
    return user.token_version


async def _reuse_entries(session: AsyncSession) -> int:
    rows = await session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.REFRESH_TOKEN_REUSED)
    )
    return len(rows.scalars().all())


async def test_a_suspended_workspace_refuses_by_its_code_and_leaves_the_token_unspent(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    session = await _register(auth, db_session)
    version = await _version(db_session)
    await _set_status(db_session, TenantStatus.SUSPENDED)

    with pytest.raises(PermissionDeniedError) as refused:
        await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)

    assert refused.value.error_code == "workspace_suspended"
    assert not await _spent(redis, session.refresh_token, settings)
    assert await _version(db_session) == version


async def test_checking_again_while_suspended_is_the_same_refusal_not_a_replay(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    session = await _register(auth, db_session)
    version = await _version(db_session)
    await _set_status(db_session, TenantStatus.SUSPENDED)

    for _ in range(3):
        with pytest.raises(PermissionDeniedError) as refused:
            await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)
        assert refused.value.error_code == "workspace_suspended"

    assert await _version(db_session) == version
    assert await _reuse_entries(db_session) == 0


async def test_after_a_restore_the_same_token_refreshes_and_rotates(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    session = await _register(auth, db_session)
    version = await _version(db_session)
    await _set_status(db_session, TenantStatus.SUSPENDED)
    with pytest.raises(PermissionDeniedError):
        await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)

    await _set_status(db_session, TenantStatus.ACTIVE)
    refreshed = await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)

    assert refreshed.refresh_token != session.refresh_token
    assert refreshed.workspace is not None and refreshed.workspace.tenant.slug == SLUG
    assert await _spent(redis, session.refresh_token, settings)
    assert await _version(db_session) == version, "no family was revoked"
    assert await _reuse_entries(db_session) == 0


async def test_a_spent_token_is_still_a_replay_when_the_workspace_refuses(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    """Reuse detection is unchanged: the workspace's refusal never hides a replay."""
    session = await _register(auth, db_session)
    version = await _version(db_session)
    await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)
    await _set_status(db_session, TenantStatus.SUSPENDED)

    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)

    assert await _version(db_session) == version + 1
    assert await _reuse_entries(db_session) == 1


async def test_a_withdrawn_membership_refuses_as_before_and_leaves_the_token_unspent(
    auth: AuthService, db_session: AsyncSession, redis: _RedisClient, settings: Settings
) -> None:
    """SU-03: with another workspace to open, a refresh into the withdrawn one answers the
    generic not-found it always did (FEB-34 records `membership_revoked` there as a later
    decision) - and the token is not spent."""
    session = await _register(auth, db_session)
    user = (
        await db_session.execute(select(User).where(User.email == "refusal@example.test"))
    ).scalar_one()
    other = Tenant(name="Other", slug=f"{SLUG}-other", timezone=TEST_TIMEZONE)
    db_session.add(other)
    await db_session.flush()
    db_session.add(Membership(tenant_id=other.id, user_id=user.id, role=TenantRole.MEMBER))
    tenant = await _tenant(db_session)
    membership = (
        await db_session.execute(
            select(Membership).where(
                Membership.tenant_id == tenant.id, Membership.user_id == user.id
            )
        )
    ).scalar_one()
    membership.status = MembershipStatus.REVOKED
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)
    assert not await _spent(redis, session.refresh_token, settings)

    membership.status = MembershipStatus.ACTIVE
    await db_session.flush()
    refreshed = await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)
    assert refreshed.refresh_token != session.refresh_token


async def test_a_sole_withdrawn_membership_still_opens_a_session_without_a_workspace(
    auth: AuthService, db_session: AsyncSession
) -> None:
    """Unchanged (recorded for FEB-34): with no workspace left, the refresh is not refused - it
    opens a session that names none, as a person with no membership gets."""
    session = await _register(auth, db_session)
    tenant = await _tenant(db_session)
    membership = (
        await db_session.execute(select(Membership).where(Membership.tenant_id == tenant.id))
    ).scalar_one()
    membership.status = MembershipStatus.REVOKED
    await db_session.flush()

    refreshed = await auth.refresh(refresh_token=session.refresh_token, workspace_slug=SLUG)
    assert refreshed.workspace is None
