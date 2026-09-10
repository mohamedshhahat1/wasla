"""Proving a linked Google identity in order to close an account.

A Google-only account has no password, and `DELETE /auth/me` needs proof beyond
a session - a stolen access token *is* a session. The previous answer was "set a
password first": secure, and a poor thing to ask of somebody on their way out.

The flow is the linking flow with a different `FlowKind`, so PKCE S256, the
nonce, the single-use server-side state, the browser binding and the fixed
redirect URI are all unchanged. What is new is what the callback does with the
identity that comes back, and the proof it leaves behind.

Everything below is about the ways that proof must not work:

* a Google account that is not the one linked here - **checked by `sub`, never
  by email**, because addresses are reassignable inside a Workspace domain and a
  person may hold several;
* a flow started by a different account;
* a proof that has expired, been spent, or belongs to somebody else;
* a proof minted for a different purpose.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.oauth_binding import hash_binding
from app.core.oauth_flow import FlowKind, OAuthFlow
from app.core.reauth import ReauthProofStore, ReauthPurpose
from app.core.security import create_access_token, hash_password
from app.db.models import Tenant, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.identity import FederatedIdentity, IdentityProvider
from app.integrations.google.oidc import GoogleIdentityClaims
from app.main import create_app
from app.services.auth_service import AuthService
from app.services.google_auth_service import GoogleAuthService
from tests.conftest import AllowingEntitlements
from tests.fakes import (
    as_flow_store,
    as_google_client,
    as_id_token_verifier,
    as_redis_client,
)

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
LINKED_SUBJECT = "109876543210987654321"
OTHER_SUBJECT = "900000000000000000001"
EMAIL = "google-person@example.com"

BROWSER_SECRET = "browser-binding-secret-for-reauth-tests"
BROWSER_BINDING = hash_binding(BROWSER_SECRET)


class _FakeRedis:
    """Enough Redis for the proof store's GETDEL pipeline."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _FakePipeline:
    """`GET` then `DELETE`, executed together - the single-use mechanism."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, key: str) -> None:
        self._ops.append(("get", key))

    def delete(self, key: str) -> None:
        self._ops.append(("delete", key))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, key in self._ops:
            if op == "get":
                results.append(self._redis.values.get(key))
            else:
                results.append(1 if self._redis.values.pop(key, None) is not None else 0)
        self._ops.clear()
        return results


class _Infra:
    def __init__(self) -> None:
        self.commands = _FakeRedis()

    @property
    def client(self) -> _FakeRedis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


class _ScriptedFlows:
    """One flow, spent once. The state store's contract in miniature."""

    def __init__(self, flow: OAuthFlow | None) -> None:
        self._flow = flow

    async def start(self, **kwargs: Any) -> Any:  # pragma: no cover - unused here
        raise AssertionError("these tests redeem an existing flow")

    async def spend(self, *, state: str) -> OAuthFlow | None:
        flow, self._flow = self._flow, None
        return flow


class _ScriptedClient:
    async def exchange(self, *, code: str, code_verifier: str) -> str:
        return "an-id-token"

    def authorization_url(self, **kwargs: Any) -> str:  # pragma: no cover
        return "https://accounts.google.com/o/oauth2/v2/auth"


class _ScriptedVerifier:
    def __init__(self, claims: GoogleIdentityClaims) -> None:
        self._claims = claims

    async def verify(self, *, id_token: str, nonce: str) -> GoogleIdentityClaims:
        return self._claims


@pytest.fixture
def reauth_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="",
    )


@pytest.fixture
def infra() -> _Infra:
    return _Infra()


@pytest.fixture
def app(reauth_settings: Settings, db_session: AsyncSession, infra: _Infra) -> Iterator[FastAPI]:
    application = create_app(reauth_settings)
    application.state.database = infra
    application.state.redis = infra

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
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


async def _google_user(
    session: AsyncSession,
    *,
    email: str = EMAIL,
    subject: str = LINKED_SUBJECT,
    password: str | None = None,
) -> User:
    """An account with a linked Google identity, and by default no password."""
    user = User(
        email=email,
        full_name="A Google Person",
        hashed_password=hash_password(password) if password else None,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    session.add(
        FederatedIdentity(
            user_id=user.id,
            provider=IdentityProvider.GOOGLE,
            provider_subject=subject,
        )
    )
    await session.flush()
    return user


def _claims(subject: str, email: str = EMAIL) -> GoogleIdentityClaims:
    return GoogleIdentityClaims(
        subject=subject,
        email=email,
        email_verified=True,
        full_name="A Google Person",
        picture=None,
        hosted_domain=None,
    )


def _flow(user_id: uuid.UUID | None, kind: FlowKind = FlowKind.REAUTH_DELETE_ACCOUNT) -> OAuthFlow:
    return OAuthFlow(
        kind=kind,
        nonce="a-nonce",
        code_verifier="a-verifier",
        binding=BROWSER_BINDING,
        user_id=user_id,
    )


def _service(
    session: AsyncSession,
    settings: Settings,
    infra: _Infra,
    *,
    flow: OAuthFlow | None,
    claims: GoogleIdentityClaims,
) -> GoogleAuthService:
    from app.core.token_store import RefreshTokenStore

    return GoogleAuthService(
        session=session,
        settings=settings,
        flows=as_flow_store(_ScriptedFlows(flow)),
        client=as_google_client(_ScriptedClient()),
        verifier=as_id_token_verifier(_ScriptedVerifier(claims)),
        auth=AuthService(
            session=session,
            settings=settings,
            token_store=RefreshTokenStore(as_redis_client(infra)),
        ),
        reauth=ReauthProofStore(as_redis_client(infra)),
    )


def _bearer(user: User, settings: Settings) -> dict[str, str]:
    token, _ = create_access_token(
        settings=settings,
        subject=user.id,
        tenant_id=None,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------- the proof


async def test_the_linked_google_identity_produces_a_usable_proof(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id),
        claims=_claims(LINKED_SUBJECT),
    )

    token = await service.complete_reauth(
        user=user,
        code="a-code",
        state="a-state",
        binding=BROWSER_SECRET,
    )

    assert token
    proof = await ReauthProofStore(as_redis_client(infra)).spend(token=token)
    assert proof is not None
    assert proof.user_id == user.id
    assert proof.purpose is ReauthPurpose.DELETE_ACCOUNT


async def test_re_authentication_is_audited_without_the_proof(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """A credential check that stands in for a password leaves a trail.

    The token itself appears nowhere: it is a bearer proof for the next five
    minutes, and a copy of it in an audit row is a copy of the authorisation.
    """
    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id),
        claims=_claims(LINKED_SUBJECT),
    )

    token = await service.complete_reauth(
        user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
    )

    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.ACCOUNT_REAUTHENTICATED)
    )
    entry = next(iter(rows.scalars()))
    assert entry.actor_id == user.id
    assert token not in str(entry.meta)


async def test_a_different_google_account_is_refused(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """Signing in with *a* Google account is not proof of controlling *this* one."""
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id),
        claims=_claims(OTHER_SUBJECT, email="someone-else@example.com"),
    )

    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
        )


async def test_the_same_email_with_a_different_subject_is_refused(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """The assertion this whole flow turns on.

    A Google address can be reassigned inside a Workspace domain, so somebody
    who controls the mailbox is not thereby the person who linked it. Identity
    is the `sub`, and a flow that compared addresses would accept exactly this.
    """
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        # Same address, different Google account.
        flow=_flow(user.id),
        claims=_claims(OTHER_SUBJECT, email=EMAIL),
    )

    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
        )


async def test_a_flow_started_by_another_account_is_refused(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """`flow.user_id` is written server-side and is the second binding."""
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    other = await _google_user(
        db_session, email="other@example.com", subject="777000111222333444555"
    )
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(other.id),
        claims=_claims(LINKED_SUBJECT),
    )

    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
        )


async def test_a_login_flow_cannot_be_completed_as_a_re_authentication(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """Why the kind is separate.

    `LOGIN` will enrol a brand-new account, so a callback accepting a login-kind
    flow as proof would accept somebody signing in with any Google account as
    authorisation to delete the account the session belongs to.
    """
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id, kind=FlowKind.LOGIN),
        claims=_claims(LINKED_SUBJECT),
    )

    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
        )


async def test_a_callback_from_the_wrong_browser_is_refused(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id),
        claims=_claims(LINKED_SUBJECT),
    )

    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding="not-the-secret"
        )


async def test_a_replayed_state_is_refused(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """Single use is the store's, and the second attempt finds nothing."""
    from app.core.exceptions import AuthenticationError

    user = await _google_user(db_session)
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=_flow(user.id),
        claims=_claims(LINKED_SUBJECT),
    )

    await service.complete_reauth(user=user, code="a-code", state="a-state", binding=BROWSER_SECRET)
    with pytest.raises(AuthenticationError):
        await service.complete_reauth(
            user=user, code="a-code", state="a-state", binding=BROWSER_SECRET
        )


# --------------------------------------------------------------- deletion


async def test_a_passwordless_account_deletes_itself_with_a_proof(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """The point of the whole feature.

    No password is set at any moment - the account leaves as it arrived.
    """
    user = await _google_user(db_session)
    assert user.hashed_password is None
    token = await ReauthProofStore(as_redis_client(infra)).issue(
        user_id=user.id,
        purpose=ReauthPurpose.DELETE_ACCOUNT,
    )

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"reauthentication_token": token},
        headers=_bearer(user, reauth_settings),
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(user)
    assert user.deleted_at is not None
    assert user.hashed_password is None
    # And the session it was made with is dead.
    assert (
        await http.get(f"{API}/auth/me", headers=_bearer(user, reauth_settings))
    ).status_code == 401


async def test_a_proof_is_single_use(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """Spent by presenting it, whether or not the deletion then succeeds."""
    user = await _google_user(db_session)
    store = ReauthProofStore(as_redis_client(infra))
    token = await store.issue(user_id=user.id, purpose=ReauthPurpose.DELETE_ACCOUNT)

    first = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"reauthentication_token": token},
        headers=_bearer(user, reauth_settings),
    )
    assert first.status_code == 200, first.text

    assert await store.spend(token=token) is None


async def test_another_users_proof_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """Checked against the caller, not trusted from the request."""
    victim = await _google_user(db_session)
    attacker = await _google_user(
        db_session, email="attacker@example.com", subject="555000111222333444555"
    )
    token = await ReauthProofStore(as_redis_client(infra)).issue(
        user_id=attacker.id,
        purpose=ReauthPurpose.DELETE_ACCOUNT,
    )

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"reauthentication_token": token},
        headers=_bearer(victim, reauth_settings),
    )

    assert response.status_code == 401, response.text
    await db_session.refresh(victim)
    assert victim.deleted_at is None


async def test_an_unknown_or_expired_proof_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
) -> None:
    """Expired, forged and already-spent are indistinguishable to a guesser."""
    user = await _google_user(db_session)

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"reauthentication_token": "not-a-real-proof"},
        headers=_bearer(user, reauth_settings),
    )

    assert response.status_code == 401, response.text
    await db_session.refresh(user)
    assert user.deleted_at is None


async def test_supplying_neither_proof_names_what_to_do(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
) -> None:
    """`reauthentication_required`, not `password_required`.

    An account with no password is not missing a password; it is missing a
    demonstration, and there are two ways to give one.
    """
    user = await _google_user(db_session)

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={},
        headers=_bearer(user, reauth_settings),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "reauthentication_required"


async def test_a_password_account_may_still_use_its_password(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
) -> None:
    """The existing path is untouched, and a Google identity does not disable it.

    Either proof, never both: requiring both from an account that has both would
    be step-up MFA, and would make the more securely configured account the
    harder one to close.
    """
    user = await _google_user(db_session, email="both@example.com", password=PASSWORD)

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"current_password": PASSWORD},
        headers=_bearer(user, reauth_settings),
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(user)
    assert user.deleted_at is not None


async def test_google_cannot_open_the_account_after_a_reauth_deletion(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """The identity row survives, so the refusal is reliable rather than a 500.

    Deleting it would free the Google `sub` while the address stayed reserved,
    and the next sign-in would try to create an account the unique constraint
    refuses.
    """
    user = await _google_user(db_session)
    token = await ReauthProofStore(as_redis_client(infra)).issue(
        user_id=user.id,
        purpose=ReauthPurpose.DELETE_ACCOUNT,
    )
    assert (
        await http.request(
            "DELETE",
            f"{API}/auth/me",
            json={"reauthentication_token": token},
            headers=_bearer(user, reauth_settings),
        )
    ).status_code == 200

    identity = await db_session.scalar(
        select(FederatedIdentity).where(FederatedIdentity.provider_subject == LINKED_SUBJECT)
    )
    assert identity is not None
    assert identity.user_id == user.id
    await db_session.refresh(user)
    assert user.deleted_at is not None


async def test_starting_a_reauth_without_a_google_identity_is_a_conflict(
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """A redirect to Google would send somebody there to be told it was pointless.

    A conflict lets a client fall back to asking for a password instead.
    """
    from app.core.exceptions import ConflictError

    user = User(
        email="password-only@example.com",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    service = _service(
        db_session,
        reauth_settings,
        infra,
        flow=None,
        claims=_claims(LINKED_SUBJECT),
    )

    with pytest.raises(ConflictError) as caught:
        await service.start_reauth(user=user, binding=BROWSER_SECRET)
    assert caught.value.error_code == "reauthentication_unavailable"


async def test_the_deletion_still_refuses_a_last_owner(
    http: AsyncClient,
    db_session: AsyncSession,
    reauth_settings: Settings,
    infra: _Infra,
) -> None:
    """A proof authorises the *identity check*, not the ownership rules.

    Somebody who re-authenticates is still the last owner of their workspace,
    and closing the account would still strand it.
    """
    from app.db.models import Membership, TenantRole
    from app.db.models.enums import TenantStatus

    user = await _google_user(db_session, email="owner-google@example.com")
    tenant = Tenant(name="Owned", slug="google-owned", status=TenantStatus.ACTIVE)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER))
    await db_session.flush()
    token = await ReauthProofStore(as_redis_client(infra)).issue(
        user_id=user.id,
        purpose=ReauthPurpose.DELETE_ACCOUNT,
    )

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"reauthentication_token": token},
        headers=_bearer(user, reauth_settings),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "account_owns_workspaces"
    await db_session.refresh(user)
    assert user.deleted_at is None
