"""The security trail for password logins and invitations, checked as it lands.

Two findings meet here, and both are about entries that were supposed to exist.

**AUTH-04.** Google logins have written `google_login_succeeded` and
`google_login_failed` since ADR-057; password logins wrote a log line and a
counter. So "who signed in to this account, and when" was answerable for
federated sessions and not for password ones - an asymmetry that is felt during
an incident, because the trail is the artefact people reach for.

**AUTH-05.** `INVITATION_ACCEPTED` and `INVITATION_REVOKED` have been in the
enum since migration 0018 and were written by no code path. The trail said an
invitation was issued and never that anybody joined through it, which is the
security-relevant half: joining a workspace is what grants access.

**Nothing here uses `db_session`, and that is the point of the file.** A refused
login raises, so its transaction is discarded on the way out - a staged row
would be rolled back with the refusal it describes. Inside the suite's shared
transaction that is invisible: the row is readable from the same session
whether or not it would ever have been committed. So every test below drives a
real application over its own connection and reads the result back through a
*different* session, which is the only arrangement in which "durable" means
anything. Rows are uniquely named and removed in a `finally`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import generate_invitation_token, hash_password
from app.db.models import (
    Membership,
    Tenant,
    TenantInvitation,
    TenantRole,
    User,
)
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.email import OutboundEmail
from app.db.models.enums import InvitationStatus, MembershipStatus
from app.db.session import Database
from app.main import create_app
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "not the password on this account"


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
        email_enabled=False,
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


async def _entries(
    maker: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
) -> list[AuditLog]:
    """Read the trail back through a session that never saw the request.

    A fresh session over its own connection, so what comes back is what was
    committed rather than what somebody's open transaction can still see.
    """
    async with maker() as session:
        statement = select(AuditLog)
        if user_id is not None:
            statement = statement.where(AuditLog.actor_id == user_id)
        if tenant_id is not None:
            statement = statement.where(AuditLog.tenant_id == tenant_id)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        return list((await session.scalars(statement)).all())


async def _cleanup(
    maker: async_sessionmaker[AsyncSession],
    *,
    emails: list[str],
    slugs: list[str],
) -> None:
    async with maker() as session:
        await session.execute(delete(OutboundEmail).where(OutboundEmail.recipient.in_(emails)))
        tenant_ids = list(
            (await session.execute(select(Tenant.id).where(Tenant.slug.in_(slugs)))).scalars()
        )
        if tenant_ids:
            await session.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(tenant_ids)))
        user_ids = list(
            (await session.execute(select(User.id).where(User.email.in_(emails)))).scalars()
        )
        if user_ids:
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await session.execute(delete(Tenant).where(Tenant.slug.in_(slugs)))
        await session.execute(delete(User).where(User.email.in_(emails)))
        await session.commit()


def _carries_no_credential(entry: AuditLog, *secrets: str) -> bool:
    """Nothing in the row is, or contains, anything that opens the account."""
    text = " ".join(
        str(value)
        for value in (entry.actor_label, entry.target_label, entry.meta, entry.action.value)
        if value is not None
    )
    return all(secret not in text for secret in secrets)


# ------------------------------------------------------------ password login


async def test_a_successful_password_login_is_recorded(prepared_database: str) -> None:
    """The entry AUTH-04 found missing, and what it may and may not carry."""
    suffix = uuid.uuid4().hex[:10]
    email = f"login-audit-{suffix}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = User(
                    email=email,
                    hashed_password=hash_password(PASSWORD),
                    is_active=True,
                    email_verified_at=datetime.now(UTC),
                )
                session.add(user)
                await session.commit()
                user_id = user.id

            async with _client(prepared_database) as client:
                response = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": PASSWORD}
                )
                assert response.status_code == 200, response.text

            entries = await _entries(maker, user_id=user_id, action=AuditAction.LOGIN_SUCCEEDED)
            assert len(entries) == 1
            entry = entries[0]
            # Their own act, and they proved it, so the actor is the person.
            assert entry.actor_kind is AuditActorKind.USER
            assert entry.actor_label == email
            assert entry.target_type == "user"
            assert entry.target_id == user_id
            assert entry.meta == {"method": "password"}
            # An account-level act: a session is opened against a global
            # identity, and whichever workspace it selects is not what this
            # entry is about.
            assert entry.tenant_id is None
            assert _carries_no_credential(entry, PASSWORD)
        finally:
            await _cleanup(maker, emails=[email], slugs=[])


@pytest.mark.parametrize(
    ("is_active", "deleted", "password", "expected_status", "expected_reason"),
    [
        # A wrong password against a live account: the ordinary case, and the
        # one worth seeing a burst of.
        (True, False, WRONG_PASSWORD, 401, "invalid_credentials"),
        # An account that exists and has been disabled. The password was
        # correct, which is exactly why this is worth recording separately.
        (False, False, PASSWORD, 403, "account_inactive"),
        # Somebody trying to sign in to an account that has been closed.
        (True, True, PASSWORD, 401, "account_deleted"),
    ],
)
async def test_a_refused_login_against_a_known_account_leaves_a_durable_entry(
    prepared_database: str,
    is_active: bool,
    deleted: bool,
    password: str,
    expected_status: int,
    expected_reason: str,
) -> None:
    """The row must survive the refusal that produced it.

    This is the whole finding. The request raises, the request's transaction is
    discarded, and a staged entry goes with it - so the trail would record
    every successful login and no failed one, which is the wrong half. The
    service commits the entry explicitly before raising, the way the
    refresh-reuse teardown and the verification service already do.

    Read back through a different session, because the point is that it was
    committed and not merely staged.
    """
    suffix = uuid.uuid4().hex[:10]
    email = f"login-refused-{suffix}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = User(
                    email=email,
                    hashed_password=hash_password(PASSWORD),
                    is_active=is_active,
                    email_verified_at=datetime.now(UTC),
                    deleted_at=datetime.now(UTC) if deleted else None,
                )
                session.add(user)
                await session.commit()
                user_id = user.id

            async with _client(prepared_database) as client:
                response = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": password}
                )
                assert response.status_code == expected_status, response.text

            entries = await _entries(maker, user_id=user_id, action=AuditAction.LOGIN_FAILED)
            assert len(entries) == 1, entries
            entry = entries[0]
            # The account did not do this; something presenting its address
            # did, and the entry must not read as an act the person took.
            assert entry.actor_kind is AuditActorKind.SYSTEM
            assert entry.actor_label == email
            assert entry.target_id == user_id
            assert entry.meta == {"method": "password", "reason": expected_reason}
            assert entry.tenant_id is None
            # Never the submitted value, never the stored hash. A trail of
            # near-misses is a trail that narrows a keyspace.
            assert _carries_no_credential(entry, password, PASSWORD, WRONG_PASSWORD)
            assert user.hashed_password is not None
            assert _carries_no_credential(entry, user.hashed_password)
        finally:
            await _cleanup(maker, emails=[email], slugs=[])


async def test_a_google_only_account_records_the_password_attempt(
    prepared_database: str,
) -> None:
    """An account with no password hash is a known account, so it is recorded.

    `login` refuses it in the same branch as an unknown address - which is what
    keeps the two indistinguishable to the caller - but the two are not the
    same thing to somebody reading the trail afterwards. Here there is an
    account to attribute the attempt to, and somebody trying passwords against
    a Google-only account is worth being able to see.
    """
    suffix = uuid.uuid4().hex[:10]
    email = f"google-only-{suffix}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = User(
                    email=email,
                    hashed_password=None,
                    is_active=True,
                    email_verified_at=datetime.now(UTC),
                )
                session.add(user)
                await session.commit()
                user_id = user.id

            async with _client(prepared_database) as client:
                response = await client.post(
                    f"{API}/auth/login", json={"email": email, "password": PASSWORD}
                )
                assert response.status_code == 401, response.text

            entries = await _entries(maker, user_id=user_id, action=AuditAction.LOGIN_FAILED)
            assert len(entries) == 1
            assert entries[0].meta == {"method": "password", "reason": "invalid_credentials"}
        finally:
            await _cleanup(maker, emails=[email], slugs=[])


async def test_an_unknown_address_writes_nothing_at_all(prepared_database: str) -> None:
    """No account, no row - and the answer is the same 401 either way.

    Two reasons, and the second is the operational one. There is no account to
    attribute the attempt to, so a row would name nobody. And `/auth/login` is
    unauthenticated, so a row anybody can cause at will is a way to flood a
    trail colleagues have to read: the per-account limiter bounds what one
    account can attract, and nothing would bound this.

    What it costs is visibility of spraying against addresses that do not
    exist. That is what `wasla_auth_security_events_total{event="login"}` and
    `PasswordLoginFailureSpike` are for - an aggregate signal for an aggregate
    problem.
    """
    suffix = uuid.uuid4().hex[:10]
    known = f"known-{suffix}@example.com"
    unknown = f"nobody-{suffix}@example.com"

    async with _sessions(prepared_database) as maker:
        try:
            async with maker() as session:
                user = User(
                    email=known,
                    hashed_password=hash_password(PASSWORD),
                    is_active=True,
                    email_verified_at=datetime.now(UTC),
                )
                session.add(user)
                await session.commit()

            async with _client(prepared_database) as client:
                hit = await client.post(
                    f"{API}/auth/login", json={"email": known, "password": WRONG_PASSWORD}
                )
                miss = await client.post(
                    f"{API}/auth/login", json={"email": unknown, "password": WRONG_PASSWORD}
                )

            # The audit asymmetry must not become a response asymmetry: the
            # caller still cannot tell the two apart.
            assert hit.status_code == miss.status_code == 401
            assert hit.json()["error"]["code"] == miss.json()["error"]["code"]
            assert hit.json()["error"]["message"] == miss.json()["error"]["message"]

            async with maker() as session:
                orphans = list(
                    (
                        await session.scalars(
                            select(AuditLog).where(
                                AuditLog.action == AuditAction.LOGIN_FAILED,
                                AuditLog.target_label == unknown,
                            )
                        )
                    ).all()
                )
            assert orphans == []
        finally:
            await _cleanup(maker, emails=[known, unknown], slugs=[])


# --------------------------------------------------------------- invitations


async def _seed_invitation(
    maker: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    inviter_email: str,
    invited_email: str,
    token_hash: str,
    status: InvitationStatus = InvitationStatus.PENDING,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with maker() as session:
        inviter = User(
            email=inviter_email,
            hashed_password=hash_password(PASSWORD),
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        tenant = Tenant(name=slug.title(), slug=slug)
        session.add_all([inviter, tenant])
        await session.flush()
        session.add(
            Membership(
                user_id=inviter.id,
                tenant_id=tenant.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.ACTIVE,
            )
        )
        invitation = TenantInvitation(
            tenant_id=tenant.id,
            email=invited_email,
            role=TenantRole.MEMBER,
            status=status,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            invited_by_id=inviter.id,
        )
        session.add(invitation)
        await session.commit()
        return tenant.id, inviter.id, invitation.id


async def test_accepting_an_invitation_is_recorded_in_the_workspace_trail(
    prepared_database: str,
) -> None:
    """`member_invited` said somebody was asked. This says somebody arrived.

    Recorded against the workspace rather than only against the platform,
    because a customer asking "who is in our workspace, and how did they get
    here" is asking about their workspace.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"invite-audit-{suffix}"
    inviter_email = f"inviter-{suffix}@example.com"
    invited_email = f"invitee-{suffix}@example.com"
    raw_token, token_hash = generate_invitation_token()

    async with _sessions(prepared_database) as maker:
        try:
            tenant_id, _, invitation_id = await _seed_invitation(
                maker,
                slug=slug,
                inviter_email=inviter_email,
                invited_email=invited_email,
                token_hash=token_hash,
            )

            async with _client(prepared_database) as client:
                response = await client.post(
                    f"{API}/invitations/accept",
                    json={"token": raw_token, "password": PASSWORD, "full_name": "Invitee"},
                )
                assert response.status_code == 200, response.text

            entries = await _entries(
                maker, tenant_id=tenant_id, action=AuditAction.INVITATION_ACCEPTED
            )
            assert len(entries) == 1
            entry = entries[0]
            assert entry.actor_kind is AuditActorKind.USER
            assert entry.actor_label == invited_email
            assert entry.target_type == "invitation"
            assert entry.target_id == invitation_id
            assert entry.target_label == invited_email
            assert entry.meta is not None
            assert entry.meta["role"] == TenantRole.MEMBER.value
            # The token is a live credential until the claim spends it, and an
            # audit entry is kept for years and read by people.
            assert _carries_no_credential(entry, raw_token, token_hash, PASSWORD)
        finally:
            await _cleanup(maker, emails=[inviter_email, invited_email], slugs=[slug])


async def test_a_replayed_invitation_token_records_no_second_acceptance(
    prepared_database: str,
) -> None:
    """One arrival per invitation, whatever the token is presented for twice.

    The entry is written past the claim, so the second caller - refused by the
    conditional UPDATE that spends the invitation - leaves nothing. A trail
    that reported two acceptances of one invitation would be describing a
    membership that was never granted twice.
    """
    suffix = uuid.uuid4().hex[:10]
    slug = f"invite-replay-{suffix}"
    inviter_email = f"inviter-{suffix}@example.com"
    invited_email = f"invitee-{suffix}@example.com"
    raw_token, token_hash = generate_invitation_token()

    async with _sessions(prepared_database) as maker:
        try:
            tenant_id, _, _ = await _seed_invitation(
                maker,
                slug=slug,
                inviter_email=inviter_email,
                invited_email=invited_email,
                token_hash=token_hash,
            )

            async with _client(prepared_database) as client:
                first = await client.post(
                    f"{API}/invitations/accept",
                    json={"token": raw_token, "password": PASSWORD, "full_name": "Invitee"},
                )
                second = await client.post(
                    f"{API}/invitations/accept",
                    json={"token": raw_token, "password": PASSWORD, "full_name": "Invitee"},
                )

            assert first.status_code == 200, first.text
            assert second.status_code == 401, second.text

            entries = await _entries(
                maker, tenant_id=tenant_id, action=AuditAction.INVITATION_ACCEPTED
            )
            assert len(entries) == 1
        finally:
            await _cleanup(maker, emails=[inviter_email, invited_email], slugs=[slug])


async def test_revoking_an_invitation_records_who_revoked_it(
    prepared_database: str,
) -> None:
    """Withdrawing somebody's way in is a decision, and decisions have owners."""
    suffix = uuid.uuid4().hex[:10]
    slug = f"revoke-audit-{suffix}"
    inviter_email = f"inviter-{suffix}@example.com"
    invited_email = f"invitee-{suffix}@example.com"
    _, token_hash = generate_invitation_token()

    async with _sessions(prepared_database) as maker:
        try:
            tenant_id, inviter_id, invitation_id = await _seed_invitation(
                maker,
                slug=slug,
                inviter_email=inviter_email,
                invited_email=invited_email,
                token_hash=token_hash,
            )

            async with _client(prepared_database) as client:
                signed_in = await client.post(
                    f"{API}/auth/login",
                    json={"email": inviter_email, "password": PASSWORD, "workspace_slug": slug},
                )
                assert signed_in.status_code == 200, signed_in.text
                headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

                revoked = await client.delete(f"{API}/invitations/{invitation_id}", headers=headers)
                assert revoked.status_code == 200, revoked.text

                # A second attempt conflicts, because there is nothing pending
                # left to revoke.
                again = await client.delete(f"{API}/invitations/{invitation_id}", headers=headers)
                assert again.status_code == 409, again.text

            entries = await _entries(
                maker, tenant_id=tenant_id, action=AuditAction.INVITATION_REVOKED
            )
            # Once, not twice: the entry sits past the conditional UPDATE, so
            # the refused second attempt records nothing.
            assert len(entries) == 1
            entry = entries[0]
            assert entry.actor_kind is AuditActorKind.USER
            assert entry.actor_id == inviter_id
            assert entry.actor_label == inviter_email
            assert entry.target_type == "invitation"
            assert entry.target_id == invitation_id
            assert entry.target_label == invited_email
            assert entry.meta == {"role": TenantRole.MEMBER.value}
            assert _carries_no_credential(entry, token_hash)
        finally:
            await _cleanup(maker, emails=[inviter_email, invited_email], slugs=[slug])


async def test_a_foreign_workspace_cannot_revoke_and_records_nothing(
    prepared_database: str,
) -> None:
    """A 404 that leaves no trace, because nothing happened.

    The trail must not become the place a refused cross-tenant attempt is
    reported as a revocation. It is also the second half of the isolation
    property: an entry appearing in the *victim's* workspace trail because a
    stranger asked would be a way to write into somebody else's audit log.
    """
    suffix = uuid.uuid4().hex[:10]
    owned = f"revoke-own-{suffix}"
    foreign = f"revoke-foreign-{suffix}"
    outsider_email = f"outsider-{suffix}@example.com"
    inviter_email = f"inviter-{suffix}@example.com"
    invited_email = f"invitee-{suffix}@example.com"
    _, token_hash = generate_invitation_token()

    async with _sessions(prepared_database) as maker:
        try:
            foreign_tenant_id, _, invitation_id = await _seed_invitation(
                maker,
                slug=foreign,
                inviter_email=inviter_email,
                invited_email=invited_email,
                token_hash=token_hash,
            )
            async with maker() as session:
                outsider = User(
                    email=outsider_email,
                    hashed_password=hash_password(PASSWORD),
                    is_active=True,
                    email_verified_at=datetime.now(UTC),
                )
                tenant = Tenant(name=owned.title(), slug=owned)
                session.add_all([outsider, tenant])
                await session.flush()
                session.add(
                    Membership(
                        user_id=outsider.id,
                        tenant_id=tenant.id,
                        role=TenantRole.TENANT_OWNER,
                        status=MembershipStatus.ACTIVE,
                    )
                )
                await session.commit()

            async with _client(prepared_database) as client:
                signed_in = await client.post(
                    f"{API}/auth/login",
                    json={"email": outsider_email, "password": PASSWORD, "workspace_slug": owned},
                )
                assert signed_in.status_code == 200, signed_in.text
                headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}

                refused = await client.delete(f"{API}/invitations/{invitation_id}", headers=headers)
                assert refused.status_code == 404, refused.text

            assert (
                await _entries(
                    maker,
                    tenant_id=foreign_tenant_id,
                    action=AuditAction.INVITATION_REVOKED,
                )
                == []
            )
            assert await _entries(maker, action=AuditAction.INVITATION_REVOKED) == []
        finally:
            await _cleanup(
                maker,
                emails=[outsider_email, inviter_email, invited_email],
                slugs=[owned, foreign],
            )
