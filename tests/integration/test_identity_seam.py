"""Where invitations, global identity and Google sign-in meet.

Three subsystems, each well covered on its own, and a defect that lived in the
gap between them (ADR-057).

`InvitationService.accept` used to set a password on any account whose
``hashed_password`` was ``None``, reasoning that passwordlessness meant "created
by an earlier invitation that was never completed". Google sign-in made that
inference false: ``GoogleAuthService._enrol`` creates accounts with no password
on purpose. So anybody who could issue an invitation - which is anybody who can
register, since registration makes you the owner of your own workspace - could
invite a Google user's address, redeem the invitation with a password of their
choosing, and sign in as them.

Neither subsystem's own tests could see it. The invitation tests never meet a
Google account; the Google tests never meet an invitation. This file is the test
that crosses, and it drives the real application over HTTP against real rows:
real services, real repositories, real Argon2, real login.

The exploit is reproduced with the token taken from the **outbox row**, not from
the API response. That is deliberate. Removing the token from the response is a
separate fix in the same commit, and a regression test that depended on it would
stop proving anything the moment somebody put the field back. This one holds
even when the attacker is assumed to hold the token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import Membership, MembershipStatus, Tenant, TenantRole, User
from app.db.models.email import OutboundEmail
from app.db.models.enums import TenantStatus
from app.db.models.identity import FederatedIdentity, IdentityProvider
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"

ATTACKER_EMAIL = "attacker@example.com"
ATTACKER_PASSWORD = "attacker chosen passphrase"
VICTIM_EMAIL = "google-victim@example.com"
VICTIM_SUBJECT = "109876543210987654321"
CHOSEN_PASSWORD = "a perfectly strong passphrase"


class _Redis:
    """Enough Redis for the token store and the limiter, behaving as Redis does."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

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

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def seam_settings() -> Settings:
    """Email on, so the invitation token lands somewhere a test can read it.

    Under `environment="test"` the production validator does not demand the
    rest of the email configuration, and nothing here sends: the outbox row is
    written on the request's own session and the worker never runs.
    """
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@wasla.test",
        app_public_url="https://app.wasla.test",
    )


@pytest.fixture
def app(seam_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(seam_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session(request: Request) -> AsyncIterator[AsyncSession]:
        # Parked where the real dependency parks it, so `CommittingRoute`
        # commits. The fixture joins with `create_savepoint`, so that commit is
        # a savepoint release the outer rollback still undoes.
        setattr(request.state, SESSION_STATE_ATTRIBUTE, db_session)
        yield db_session

    application.dependency_overrides[get_session] = _session
    # Seats are not what this file is about, and a starter plan allows two.
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


async def _workspace(
    session: AsyncSession,
    *,
    user: User,
    slug: str,
    role: TenantRole = TenantRole.TENANT_OWNER,
) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role))
    await session.flush()
    return tenant


async def _google_only_account(session: AsyncSession) -> User:
    """Exactly what `GoogleAuthService._enrol` writes: no password, one identity."""
    user = User(
        email=VICTIM_EMAIL,
        full_name="A Google Person",
        hashed_password=None,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    session.add(
        FederatedIdentity(
            user_id=user.id,
            provider=IdentityProvider.GOOGLE,
            provider_subject=VICTIM_SUBJECT,
        )
    )
    await session.flush()
    return user


async def _attacker(session: AsyncSession) -> User:
    user = User(
        email=ATTACKER_EMAIL,
        full_name="Somebody Else",
        hashed_password=hash_password(ATTACKER_PASSWORD),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await _workspace(session, user=user, slug="attackerspace")
    return user


async def _sign_in(http: AsyncClient, email: str, password: str) -> str:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def _invitation_token(session: AsyncSession, *, recipient: str) -> str:
    """The token as the invited mailbox receives it.

    The outbox row is the only place it exists after issuing - it is stored as
    a hash, and the API no longer returns it - so this is also the assertion
    that delivery still carries what acceptance needs.
    """
    row = (
        await session.execute(
            select(OutboundEmail)
            .where(OutboundEmail.recipient == recipient)
            .where(OutboundEmail.template == "workspace_invitation")
        )
    ).scalar_one()
    token = row.context.get("token")
    assert isinstance(token, str) and token, "the invitation email carried no token"
    return token


# ------------------------------------------------- SEC-01: the takeover, closed


async def test_an_invitation_cannot_set_a_password_on_a_google_only_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The whole exploit, end to end, refused at the step that mattered.

    Everything up to acceptance is allowed and expected: inviting an address is
    an ordinary thing for a workspace to do, and the invited person joining is
    the point of the feature. What must not happen is the global account being
    claimed on the way through.
    """
    victim = await _google_only_account(db_session)
    victim_space = await _workspace(db_session, user=victim, slug="victimspace")
    await _attacker(db_session)

    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)
    headers = {"Authorization": f"Bearer {token}"}

    issued = await http.post(
        f"{API}/invitations",
        json={"email": VICTIM_EMAIL, "role": "member"},
        headers=headers,
    )
    assert issued.status_code == 201, issued.text

    raw_token = await _invitation_token(db_session, recipient=VICTIM_EMAIL)
    accepted = await http.post(
        f"{API}/invitations/accept",
        json={"token": raw_token, "password": CHOSEN_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text

    await db_session.refresh(victim)

    # 1. No password was written.
    assert victim.hashed_password is None
    # 2. And therefore the chosen one cannot be used, whatever else happened.
    refused = await http.post(
        f"{API}/auth/login",
        json={"email": VICTIM_EMAIL, "password": CHOSEN_PASSWORD},
    )
    assert refused.status_code == 401

    # 3. The Google identity is untouched, so the real owner still signs in.
    identity = (
        await db_session.execute(
            select(FederatedIdentity).where(FederatedIdentity.user_id == victim.id)
        )
    ).scalar_one()
    assert identity.provider_subject == VICTIM_SUBJECT

    # 4. No other global-account security state moved.
    assert victim.is_active is True
    assert victim.token_version == 1

    # 5. Their own workspace is still theirs, at the role they held.
    own = (
        await db_session.execute(
            select(Membership)
            .where(Membership.user_id == victim.id)
            .where(Membership.tenant_id == victim_space.id)
        )
    ).scalar_one()
    assert own.status is MembershipStatus.ACTIVE
    assert own.role is TenantRole.TENANT_OWNER


async def test_the_invitation_still_grants_the_membership_it_is_for(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Refusing the password must not refuse the invitation.

    The control for the test above: the fix has to close the takeover without
    breaking the feature, so an existing account invited to a workspace still
    joins it.
    """
    victim = await _google_only_account(db_session)
    attacker = await _attacker(db_session)
    inviter_space = (
        await db_session.execute(
            select(Tenant).join(Membership).where(Membership.user_id == attacker.id)
        )
    ).scalar_one()

    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)
    await http.post(
        f"{API}/invitations",
        json={"email": VICTIM_EMAIL, "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_token = await _invitation_token(db_session, recipient=VICTIM_EMAIL)

    accepted = await http.post(
        f"{API}/invitations/accept",
        json={"token": raw_token, "password": CHOSEN_PASSWORD},
    )

    assert accepted.status_code == 200
    assert accepted.json()["workspace"]["slug"] == inviter_space.slug
    joined = (
        await db_session.execute(
            select(Membership)
            .where(Membership.user_id == victim.id)
            .where(Membership.tenant_id == inviter_space.id)
        )
    ).scalar_one()
    assert joined.status is MembershipStatus.ACTIVE
    assert joined.role is TenantRole.MEMBER


async def test_an_invitation_cannot_replace_an_existing_password(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The same rule for an ordinary password account.

    This one was never exploitable - the old branch required a null hash - but
    the property it depends on is now stated rather than implied, so a future
    edit that reintroduces password-setting for existing accounts fails here
    too.
    """
    existing = User(
        email="settled@example.com",
        hashed_password=hash_password("the original passphrase"),
        is_active=True,
    )
    db_session.add(existing)
    await db_session.flush()
    original = existing.hashed_password

    await _attacker(db_session)
    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)
    await http.post(
        f"{API}/invitations",
        json={"email": "settled@example.com", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_token = await _invitation_token(db_session, recipient="settled@example.com")

    accepted = await http.post(
        f"{API}/invitations/accept",
        json={"token": raw_token, "password": CHOSEN_PASSWORD},
    )

    assert accepted.status_code == 200
    await db_session.refresh(existing)
    assert existing.hashed_password == original
    assert verify_password(password="the original passphrase", password_hash=original or "")


async def test_an_invitation_to_a_new_address_still_creates_the_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The one case that may set a password: the account this call created."""
    await _attacker(db_session)
    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)
    await http.post(
        f"{API}/invitations",
        json={"email": "brand-new@example.com", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_token = await _invitation_token(db_session, recipient="brand-new@example.com")

    accepted = await http.post(
        f"{API}/invitations/accept",
        json={"token": raw_token, "password": CHOSEN_PASSWORD},
    )
    assert accepted.status_code == 200

    created = (
        await db_session.execute(select(User).where(User.email == "brand-new@example.com"))
    ).scalar_one()
    assert created.hashed_password is not None
    signed_in = await http.post(
        f"{API}/auth/login",
        json={"email": "brand-new@example.com", "password": CHOSEN_PASSWORD},
    )
    assert signed_in.status_code == 200


async def test_a_spent_invitation_cannot_be_replayed(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Unchanged by the fix, and asserted here because it sits on the same path."""
    await _google_only_account(db_session)
    await _attacker(db_session)
    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)
    await http.post(
        f"{API}/invitations",
        json={"email": VICTIM_EMAIL, "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_token = await _invitation_token(db_session, recipient=VICTIM_EMAIL)

    first = await http.post(f"{API}/invitations/accept", json={"token": raw_token})
    second = await http.post(f"{API}/invitations/accept", json={"token": raw_token})

    assert first.status_code == 200
    assert second.status_code == 401


# ------------------------------------------- SEC-02: the token stops at the outbox


async def test_issuing_does_not_return_the_token_but_still_mails_it(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The credential travels to the mailbox and nowhere else (ADR-057)."""
    await _attacker(db_session)
    token = await _sign_in(http, ATTACKER_EMAIL, ATTACKER_PASSWORD)

    issued = await http.post(
        f"{API}/invitations",
        json={"email": "invitee@example.com", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert issued.status_code == 201
    body = issued.json()
    assert "token" not in body
    raw_token = await _invitation_token(db_session, recipient="invitee@example.com")
    # Not merely absent under that key: absent from the response entirely.
    assert raw_token not in issued.text
    # And what was mailed still opens the invitation.
    accepted = await http.post(
        f"{API}/invitations/accept",
        json={"token": raw_token, "password": CHOSEN_PASSWORD},
    )
    assert accepted.status_code == 200


# --------------------------------------- BUG-002: the route that should exist


async def test_a_google_first_account_can_set_a_password_and_then_sign_in(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The legitimate operation the invitation hole was standing in for.

    A session is the proof, because an account that has never had a password
    has nothing else to prove with.
    """
    victim = await _google_only_account(db_session)
    await _workspace(db_session, user=victim, slug="victimspace")

    # A Google-first account holds a session without ever having had a password.
    before_version = victim.token_version
    access, _ = create_access_token(
        settings=Settings(_env_file=None, environment="test"),
        subject=victim.id,
        tenant_id=None,
        token_version=victim.token_version,
    )
    headers = {"Authorization": f"Bearer {access}"}

    response = await http.post(
        f"{API}/auth/password/set",
        json={"new_password": CHOSEN_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # Sessions ended, exactly as a password change does.
    assert body["token_version"] == before_version + 1

    await db_session.refresh(victim)
    assert victim.hashed_password is not None
    signed_in = await http.post(
        f"{API}/auth/login",
        json={"email": VICTIM_EMAIL, "password": CHOSEN_PASSWORD},
    )
    assert signed_in.status_code == 200

    # The Google identity survives, so the account now has two ways in.
    identity = (
        await db_session.execute(
            select(FederatedIdentity).where(FederatedIdentity.user_id == victim.id)
        )
    ).scalar_one()
    assert identity.provider_subject == VICTIM_SUBJECT


async def test_setting_a_password_lets_google_be_disconnected(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`unlink` refuses while there is no other way in, and tells you to set one.

    Before this endpoint existed that sentence named an operation the API did
    not have. This asserts the instruction is now followable.
    """
    victim = await _google_only_account(db_session)
    settings = Settings(
        _env_file=None,
        environment="test",
        google_enabled=True,
        google_client_id="1234567890-testclient.apps.googleusercontent.com",
        google_client_secret="a-test-client-secret",
        google_redirect_uri="https://app.wasla.test/auth/google/callback",
    )
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session(request: Request) -> AsyncIterator[AsyncSession]:
        setattr(request.state, SESSION_STATE_ATTRIBUTE, db_session)
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        before, _ = create_access_token(
            settings=settings,
            subject=victim.id,
            tenant_id=None,
            token_version=victim.token_version,
        )
        refused = await client.delete(
            f"{API}/auth/identities/google",
            headers={"Authorization": f"Bearer {before}"},
        )
        assert refused.status_code == 403

        await client.post(
            f"{API}/auth/password/set",
            json={"new_password": CHOSEN_PASSWORD},
            headers={"Authorization": f"Bearer {before}"},
        )
        await db_session.refresh(victim)
        after, _ = create_access_token(
            settings=settings,
            subject=victim.id,
            tenant_id=None,
            token_version=victim.token_version,
        )
        allowed = await client.delete(
            f"{API}/auth/identities/google",
            headers={"Authorization": f"Bearer {after}"},
        )

    assert allowed.status_code == 204


async def test_password_set_is_not_a_second_way_to_replace_a_password(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An account that already has one is refused. This is not a reset."""
    settled = User(
        email="settled@example.com",
        hashed_password=hash_password("the original passphrase"),
        is_active=True,
    )
    db_session.add(settled)
    await db_session.flush()
    original = settled.hashed_password
    access, _ = create_access_token(
        settings=Settings(_env_file=None, environment="test"),
        subject=settled.id,
        tenant_id=None,
        token_version=settled.token_version,
    )

    response = await http.post(
        f"{API}/auth/password/set",
        json={"new_password": CHOSEN_PASSWORD},
        headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 422
    await db_session.refresh(settled)
    assert settled.hashed_password == original


async def test_password_set_requires_a_session(http: AsyncClient) -> None:
    response = await http.post(
        f"{API}/auth/password/set",
        json={"new_password": CHOSEN_PASSWORD},
    )
    assert response.status_code == 401


async def test_password_set_enforces_the_strength_policy(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    victim = await _google_only_account(db_session)
    access, _ = create_access_token(
        settings=Settings(_env_file=None, environment="test"),
        subject=victim.id,
        tenant_id=None,
        token_version=victim.token_version,
    )

    response = await http.post(
        f"{API}/auth/password/set",
        json={"new_password": "short"},
        headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 422
    await db_session.refresh(victim)
    assert victim.hashed_password is None


async def test_one_account_cannot_set_another_accounts_password(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """There is no target in the request, which is what makes this true.

    Asserted anyway: the endpoint acts on the authenticated account and takes
    no identifier, so an attacker holding their own session cannot aim it.
    """
    victim = await _google_only_account(db_session)
    attacker = await _attacker(db_session)
    access, _ = create_access_token(
        settings=Settings(_env_file=None, environment="test"),
        subject=attacker.id,
        tenant_id=None,
        token_version=attacker.token_version,
    )
    response = await http.post(
        f"{API}/auth/password/set",
        json={"new_password": CHOSEN_PASSWORD, "user_id": str(victim.id)},
        headers={"Authorization": f"Bearer {access}"},
    )

    # `extra="forbid"`: naming a target is a 422 rather than a field ignored.
    assert response.status_code == 422
    await db_session.refresh(victim)
    assert victim.hashed_password is None
