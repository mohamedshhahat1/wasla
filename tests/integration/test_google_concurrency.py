"""OAuth collision invariants with independent PostgreSQL transactions and Redis."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.core.oauth_binding import hash_binding
from app.core.oauth_flow import FlowKind, OAuthFlowStore, StartedFlow
from app.core.reauth import ReauthProofStore
from app.core.redis import RedisClient
from app.core.security import hash_password
from app.core.token_store import RefreshTokenStore
from app.db.models import FederatedIdentity, Tenant, User
from app.db.session import Database
from app.integrations.google.client import GoogleOAuthClient
from app.integrations.google.oidc import GoogleIdTokenVerifier
from app.services.auth_service import AuthService, EmailAlreadyRegisteredError
from app.services.google_auth_service import GoogleAuthService
from tests.integration.test_google_endpoints import (
    CLIENT_ID,
    REDIRECT_URI,
    _FixedKeyRing,
    _id_token,
)

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


class _Exchange(GoogleOAuthClient):
    tokens: ClassVar[dict[str, str]] = {}

    async def exchange(self, *, code: str, code_verifier: str) -> str:
        del code_verifier
        return self.tokens[code]


@dataclass(slots=True)
class RaceContext:
    database: Database
    redis: RedisClient
    settings: Settings
    states: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    slugs: list[str] = field(default_factory=list)

    @property
    def flows(self) -> OAuthFlowStore:
        return OAuthFlowStore(self.redis)

    def service(self, session: AsyncSession) -> GoogleAuthService:
        client = _Exchange(
            client_id=CLIENT_ID,
            client_secret="test-secret",
            redirect_uri=REDIRECT_URI,
        )
        return GoogleAuthService(
            session=session,
            settings=self.settings,
            flows=self.flows,
            client=client,
            verifier=GoogleIdTokenVerifier(client_id=CLIENT_ID, key_ring=_FixedKeyRing()),
            auth=AuthService(
                session=session,
                settings=self.settings,
                token_store=RefreshTokenStore(self.redis),
            ),
            reauth=ReauthProofStore(self.redis),
        )

    async def start(
        self, *, kind: FlowKind, binding: str, user_id: uuid.UUID | None = None
    ) -> StartedFlow:
        started = await self.flows.start(kind=kind, binding=hash_binding(binding), user_id=user_id)
        self.states.append(started.state)
        return started

    def token(self, *, code: str, flow: StartedFlow, email: str, subject: str) -> None:
        _Exchange.tokens[code] = _id_token(
            nonce=flow.flow.nonce,
            email=email,
            sub=subject,
            hd="example.com",
        )

    async def login(self, *, code: str, flow: StartedFlow, binding: str) -> str:
        try:
            async with self.database.session() as session:
                await self.service(session).complete_login(
                    code=code, state=flow.state, binding=binding
                )
            return "success"
        except ConflictError:
            return "conflict"


@pytest_asyncio.fixture
async def race(prepared_database: str) -> AsyncIterator[RaceContext]:
    redis_url = os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        pytest.skip("real Redis URL is required for OAuth concurrency tests")
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=prepared_database,
        redis_url=redis_url,
        jwt_secret="oauth-concurrency-test-signing-key-32-bytes",
        rate_limit_enabled=False,
        google_enabled=True,
        google_client_id=CLIENT_ID,
        google_client_secret="test-secret",
        google_redirect_uri=REDIRECT_URI,
    )
    redis = RedisClient(settings)
    await redis.check()
    database = Database(settings)
    context = RaceContext(database=database, redis=redis, settings=settings)
    try:
        yield context
    finally:
        for state in context.states:
            await redis.client.delete(f"auth:oauth:flow:{state}")
        if context.emails or context.subjects or context.slugs:
            async with database.session() as session:
                if context.subjects:
                    await session.execute(
                        delete(FederatedIdentity).where(
                            FederatedIdentity.provider_subject.in_(context.subjects)
                        )
                    )
                if context.slugs:
                    await session.execute(delete(Tenant).where(Tenant.slug.in_(context.slugs)))
                if context.emails:
                    await session.execute(delete(User).where(User.email.in_(context.emails)))
        _Exchange.tokens.clear()
        await redis.close()
        await database.dispose()


async def test_one_concurrent_consumer_spends_an_oauth_state(race: RaceContext) -> None:
    flow = await race.start(kind=FlowKind.LOGIN, binding="one-browser")
    first, second = await asyncio.gather(
        race.flows.spend(state=flow.state), race.flows.spend(state=flow.state)
    )
    assert sum(item is not None for item in (first, second)) == 1
    assert await race.flows.spend(state=flow.state) is None


async def test_two_first_logins_for_one_subject_create_one_identity(race: RaceContext) -> None:
    suffix = uuid.uuid4().hex[:10]
    email, subject = f"race-{suffix}@example.com", f"subject-{suffix}"
    race.emails.append(email)
    race.subjects.append(subject)
    first = await race.start(kind=FlowKind.LOGIN, binding="browser-one")
    second = await race.start(kind=FlowKind.LOGIN, binding="browser-two")
    race.token(code="code-one", flow=first, email=email, subject=subject)
    race.token(code="code-two", flow=second, email=email, subject=subject)

    outcomes = await asyncio.gather(
        race.login(code="code-one", flow=first, binding="browser-one"),
        race.login(code="code-two", flow=second, binding="browser-two"),
    )
    assert "success" in outcomes
    assert set(outcomes) <= {"success", "conflict"}
    async with race.database.session() as session:
        users = (await session.scalars(select(User).where(User.email == email))).all()
        identities = (
            await session.scalars(
                select(FederatedIdentity).where(FederatedIdentity.provider_subject == subject)
            )
        ).all()
        assert len(users) == len(identities) == 1
        assert identities[0].user_id == users[0].id


async def test_registration_and_google_login_cannot_auto_link_by_email(race: RaceContext) -> None:
    suffix = uuid.uuid4().hex[:10]
    email, subject, slug = (
        f"collision-{suffix}@example.com",
        f"subject-{suffix}",
        f"collision-{suffix}",
    )
    race.emails.append(email)
    race.subjects.append(subject)
    race.slugs.append(slug)
    flow = await race.start(kind=FlowKind.LOGIN, binding="google-browser")
    race.token(code="google-code", flow=flow, email=email, subject=subject)

    async def register() -> str:
        try:
            async with race.database.session() as session:
                await AuthService(
                    session=session,
                    settings=race.settings,
                    token_store=RefreshTokenStore(race.redis),
                ).register(
                    email=email, password=PASSWORD, workspace_name="Collision", workspace_slug=slug
                )
            return "registered"
        except EmailAlreadyRegisteredError:
            return "taken"

    await asyncio.gather(
        register(), race.login(code="google-code", flow=flow, binding="google-browser")
    )
    async with race.database.session() as session:
        users = (await session.scalars(select(User).where(User.email == email))).all()
        identities = (
            await session.scalars(
                select(FederatedIdentity).where(FederatedIdentity.provider_subject == subject)
            )
        ).all()
        assert len(users) == 1
        assert len(identities) <= 1
        if users[0].hashed_password is not None:
            assert not identities, "Google must never attach itself to a password account by email"
        else:
            assert len(identities) == 1
            assert identities[0].user_id == users[0].id


async def test_link_and_fresh_login_compete_for_subject_without_takeover(race: RaceContext) -> None:
    suffix = uuid.uuid4().hex[:10]
    local_email = f"local-{suffix}@example.com"
    google_email = f"google-{suffix}@example.com"
    subject = f"subject-{suffix}"
    race.emails.extend([local_email, google_email])
    race.subjects.append(subject)
    async with race.database.session() as session:
        local = User(email=local_email, hashed_password=hash_password(PASSWORD))
        session.add(local)
        await session.flush()
        local_id = local.id

    link = await race.start(kind=FlowKind.LINK, binding="link-browser", user_id=local_id)
    login = await race.start(kind=FlowKind.LOGIN, binding="login-browser")
    race.token(code="link-code", flow=link, email=google_email, subject=subject)
    race.token(code="login-code", flow=login, email=google_email, subject=subject)

    async def complete_link() -> str:
        try:
            async with race.database.session() as session:
                user = await session.get(User, local_id)
                assert user is not None
                await race.service(session).complete_link(
                    user=user, code="link-code", state=link.state, binding="link-browser"
                )
            return "linked"
        except ConflictError:
            return "conflict"

    outcomes = await asyncio.gather(
        complete_link(), race.login(code="login-code", flow=login, binding="login-browser")
    )
    assert "linked" in outcomes or "success" in outcomes
    async with race.database.session() as session:
        identity = (
            await session.scalars(
                select(FederatedIdentity).where(FederatedIdentity.provider_subject == subject)
            )
        ).one()
        local_record = await session.get(User, local_id)
        assert local_record is not None and local_record.hashed_password is not None
        if identity.user_id != local_id:
            owner = await session.get(User, identity.user_id)
            assert owner is not None and owner.email == google_email
