"""Committed PostgreSQL races for the authentication remediation.

These tests intentionally do not use ``db_session``: its outer transaction is
excellent isolation for ordinary tests and cannot represent two requests that
commit independently. Every row here is uniquely named and removed in a
``finally`` block.
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
from app.core.security import generate_invitation_token, hash_password
from app.db.models import (
    EmailVerificationChallenge,
    Membership,
    Tenant,
    TenantInvitation,
    TenantRole,
    User,
)
from app.db.models.email import OutboundEmail
from app.db.models.enums import InvitationStatus
from app.db.session import Database
from app.main import create_app
from app.repositories import TenantRepository, UserRepository
from app.repositories.invitation_repository import InvitationRepository, InvitationTokenRepository
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


class _Redis:
    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


def _settings(database_url: str, *, email: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=email,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        credential_encryption_keys=[TEST_CREDENTIAL_ENCRYPTION_KEY],
    )


@asynccontextmanager
async def _client(database_url: str, *, email: bool = True) -> AsyncIterator[AsyncClient]:
    settings = _settings(database_url, email=email)
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


@pytest.mark.parametrize(
    ("first_email", "second_email"),
    [
        ("race@example.com", "race@example.com"),
        ("Race.Case@example.com", "race.case@EXAMPLE.com"),
        ("space.race@example.com", " space.race@example.com "),
    ],
)
async def test_concurrent_duplicate_registration_is_created_and_conflict(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
    first_email: str,
    second_email: str,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    canonical = first_email.strip().lower()
    slugs = (f"auth-race-a-{suffix}", f"auth-race-b-{suffix}")
    barrier = asyncio.Barrier(2)
    original = UserRepository.create

    async def synchronized_create(self: UserRepository, **kwargs: object) -> User:
        row = await original(self, **kwargs)  # type: ignore[arg-type]
        await barrier.wait()
        return row

    monkeypatch.setattr(UserRepository, "create", synchronized_create)

    async def register(email: str, slug: str) -> int:
        async with _client(prepared_database) as client:
            response = await client.post(
                f"{API}/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": slug,
                    "workspace_slug": slug,
                },
            )
            return response.status_code

    query_database = Database(_settings(prepared_database))
    maker = async_sessionmaker(query_database.engine, expire_on_commit=False)
    try:
        outcomes = await asyncio.gather(
            register(first_email, slugs[0]),
            register(second_email, slugs[1]),
        )
        assert sorted(outcomes) == [201, 409]

        async with maker() as session:
            user_count = await session.scalar(
                select(func.count()).select_from(User).where(User.email == canonical)
            )
            tenants = list(
                (await session.execute(select(Tenant).where(Tenant.slug.in_(slugs)))).scalars()
            )
            assert user_count == 1
            assert len(tenants) == 1
            tenant_id = tenants[0].id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.tenant_id == tenant_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EmailVerificationChallenge)
                    .join(User)
                    .where(User.email == canonical)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(OutboundEmail)
                    .join(User)
                    .where(User.email == canonical)
                )
                == 1
            )
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(OutboundEmail).where(OutboundEmail.recipient == canonical))
            await cleanup.execute(delete(Tenant).where(Tenant.slug.in_(slugs)))
            await cleanup.execute(delete(User).where(User.email == canonical))
            await cleanup.commit()
        await query_database.dispose()


async def test_concurrent_workspace_slug_registration_is_controlled(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    slug = f"auth-slug-race-{suffix}"
    emails = (f"slug-a-{suffix}@example.com", f"slug-b-{suffix}@example.com")
    barrier = asyncio.Barrier(2)
    original = TenantRepository.create

    async def synchronized_create(self: TenantRepository, **kwargs: object) -> Tenant:
        row = await original(self, **kwargs)  # type: ignore[arg-type]
        await barrier.wait()
        return row

    monkeypatch.setattr(TenantRepository, "create", synchronized_create)

    async def register(email: str) -> int:
        async with _client(prepared_database) as client:
            response = await client.post(
                f"{API}/auth/register",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "workspace_name": slug,
                    "workspace_slug": slug,
                },
            )
            return response.status_code

    database = Database(_settings(prepared_database))
    maker = database.session_factory
    try:
        outcomes = await asyncio.gather(*(register(email) for email in emails))
        assert sorted(outcomes) == [201, 409]
        async with maker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Tenant).where(Tenant.slug == slug)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(User).where(User.email.in_(emails))
                )
                == 1
            )
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(OutboundEmail).where(OutboundEmail.recipient.in_(emails)))
            await cleanup.execute(delete(Tenant).where(Tenant.slug == slug))
            await cleanup.execute(delete(User).where(User.email.in_(emails)))
            await cleanup.commit()
        await database.dispose()


@pytest.mark.parametrize("existing_account", [False, True])
async def test_concurrent_invitation_acceptance_has_one_winner(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
    existing_account: bool,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    slug = f"invite-race-{suffix}"
    invited_email = f"invitee-{suffix}@example.com"
    inviter_email = f"inviter-{suffix}@example.com"
    raw_token, token_hash = generate_invitation_token()
    database = Database(_settings(prepared_database, email=False))
    maker = database.session_factory

    async with maker() as setup:
        inviter = User(
            email=inviter_email,
            hashed_password=hash_password(PASSWORD),
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        tenant = Tenant(name=slug, slug=slug)
        setup.add_all([inviter, tenant])
        await setup.flush()
        setup.add(
            TenantInvitation(
                tenant_id=tenant.id,
                email=invited_email,
                role=TenantRole.MEMBER,
                status=InvitationStatus.PENDING,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                invited_by_id=inviter.id,
            )
        )
        if existing_account:
            setup.add(
                User(
                    email=invited_email,
                    hashed_password=hash_password(PASSWORD),
                    is_active=True,
                )
            )
        await setup.commit()

    barrier = asyncio.Barrier(2)
    original = InvitationTokenRepository.get_by_token_hash

    async def synchronized_lookup(
        self: InvitationTokenRepository, token_hash: str
    ) -> TenantInvitation | None:
        row = await original(self, token_hash)
        await barrier.wait()
        return row

    monkeypatch.setattr(InvitationTokenRepository, "get_by_token_hash", synchronized_lookup)

    async def accept() -> int:
        async with _client(prepared_database, email=False) as client:
            response = await client.post(
                f"{API}/invitations/accept",
                json={"token": raw_token, "password": PASSWORD, "full_name": "Invitee"},
            )
            return response.status_code

    try:
        outcomes = await asyncio.gather(accept(), accept())
        assert sorted(outcomes) == [200, 401]
        async with maker() as session:
            invitation = (
                await session.execute(
                    select(TenantInvitation).where(TenantInvitation.token_hash == token_hash)
                )
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.email == invited_email))
            ).scalar_one()
            memberships = list(
                (
                    await session.execute(
                        select(Membership).where(
                            Membership.tenant_id == invitation.tenant_id,
                            Membership.user_id == user.id,
                        )
                    )
                ).scalars()
            )
            assert invitation.status is InvitationStatus.ACCEPTED
            assert invitation.accepted_at is not None
            assert len(memberships) == 1
            assert memberships[0].role is TenantRole.MEMBER
            assert (
                await session.scalar(
                    select(func.count()).select_from(User).where(User.email == invited_email)
                )
                == 1
            )
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Tenant).where(Tenant.slug == slug))
            await cleanup.execute(
                delete(User).where(User.email.in_((inviter_email, invited_email)))
            )
            await cleanup.commit()
        await database.dispose()


@pytest.mark.parametrize("active_after_delete", [True, False])
async def test_invitation_acceptance_never_reactivates_a_deleted_identity(
    prepared_database: str,
    active_after_delete: bool,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    slug = f"deleted-invite-{suffix}"
    email = f"deleted-invitee-{suffix}@example.com"
    raw_token, token_hash = generate_invitation_token()
    database = Database(_settings(prepared_database, email=False))
    maker = database.session_factory
    async with maker() as setup:
        user = User(
            email=email,
            hashed_password=hash_password(PASSWORD),
            is_active=active_after_delete,
            deleted_at=datetime.now(UTC),
        )
        tenant = Tenant(name=slug, slug=slug)
        setup.add_all([user, tenant])
        await setup.flush()
        setup.add(
            TenantInvitation(
                tenant_id=tenant.id,
                email=email,
                role=TenantRole.TENANT_ADMIN,
                status=InvitationStatus.PENDING,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await setup.commit()

    try:
        async with _client(prepared_database, email=False) as client:
            response = await client.post(
                f"{API}/invitations/accept",
                json={"token": raw_token, "password": "attacker-chosen-password"},
            )
        assert response.status_code == 401
        async with maker() as session:
            reloaded = await session.scalar(select(User).where(User.email == email))
            assert reloaded is not None
            assert reloaded.deleted_at is not None
            assert reloaded.is_active is active_after_delete
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.user_id == reloaded.id)
                )
                == 0
            )
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Tenant).where(Tenant.slug == slug))
            await cleanup.execute(delete(User).where(User.email == email))
            await cleanup.commit()
        await database.dispose()


async def test_accept_and_revoke_race_has_exactly_one_state_winner(
    prepared_database: str,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    slug = f"accept-revoke-{suffix}"
    email = f"accept-revoke-{suffix}@example.com"
    raw_token, token_hash = generate_invitation_token()
    database = Database(_settings(prepared_database, email=False))
    maker = database.session_factory
    async with maker() as setup:
        tenant = Tenant(name=slug, slug=slug)
        setup.add(tenant)
        await setup.flush()
        invitation = TenantInvitation(
            tenant_id=tenant.id,
            email=email,
            role=TenantRole.MEMBER,
            status=InvitationStatus.PENDING,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        setup.add(invitation)
        await setup.commit()
        invitation_id = invitation.id
        tenant_id = tenant.id

    barrier = asyncio.Barrier(2)

    async def accept() -> str:
        async with maker() as session:
            await barrier.wait()
            try:
                claimed = await InvitationTokenRepository(session).claim(
                    token_hash=token_hash,
                    now=datetime.now(UTC),
                )
                await session.commit()
                return "accepted" if claimed is not None else "lost"
            except Exception:
                await session.rollback()
                raise

    async def revoke() -> str:
        async with maker() as session:
            await barrier.wait()
            row = await InvitationRepository(session, tenant_id=tenant_id).revoke_pending(
                invitation_id
            )
            await session.commit()
            return "revoked" if row is not None else "lost"

    try:
        outcomes = await asyncio.gather(accept(), revoke())
        assert (outcomes[0] == "accepted" and outcomes[1] == "lost") or (
            outcomes[0] == "lost" and outcomes[1] == "revoked"
        )
        async with maker() as session:
            final = await session.get(TenantInvitation, invitation_id)
            assert final is not None
            assert final.status in {InvitationStatus.ACCEPTED, InvitationStatus.REVOKED}
            assert (final.accepted_at is not None) is (final.status is InvitationStatus.ACCEPTED)
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Tenant).where(Tenant.slug == slug))
            await cleanup.commit()
        await database.dispose()


async def test_invitation_is_not_claimable_at_its_expiry_boundary(
    db_session: AsyncSession,
) -> None:
    raw_token, token_hash = generate_invitation_token()
    del raw_token
    tenant = Tenant(name="Expiry", slug=f"expiry-{uuid.uuid4().hex[:10]}")
    db_session.add(tenant)
    await db_session.flush()
    expires_at = datetime.now(UTC)
    invitation = TenantInvitation(
        tenant_id=tenant.id,
        email="expiry@example.com",
        role=TenantRole.MEMBER,
        status=InvitationStatus.PENDING,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db_session.add(invitation)
    await db_session.flush()

    claimed = await InvitationTokenRepository(db_session).claim(
        token_hash=token_hash,
        now=expires_at,
    )

    assert claimed is None
    assert invitation.status is InvitationStatus.PENDING
