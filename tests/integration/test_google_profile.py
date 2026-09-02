"""The Google profile, from a token to a column and back, on a real database.

`tests/unit/test_google_profile.py` proves the refresh *rule* against a
detached object. This file proves the parts a detached object cannot: that the
column exists and round-trips, that a first login writes it inside the same
transaction as the account, that a second login updates it, and that the
address the account is reachable at does not move when Google's copy does.

Google itself is stubbed. What arrives from Google is settled by
`test_google_oidc.py`, which mints real RS256 tokens against real key sets;
repeating any of that here would be testing the stub.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.oauth_binding import hash_binding
from app.core.oauth_flow import FlowKind, OAuthFlow
from app.core.token_store import RefreshTokenStore
from app.db.models.identity import FederatedIdentity, IdentityProvider
from app.db.models.user import User
from app.integrations.google.oidc import GoogleIdentityClaims
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.google_auth_service import GoogleAuthService

pytestmark = pytest.mark.integration

SUBJECT = "109876543210987654321"
EMAIL = "person@example.com"
NAME = "A Person"
PICTURE = "https://lh3.googleusercontent.com/a/abc123=s96-c"
MOVED_PICTURE = "https://lh3.googleusercontent.com/a/new=s96-c"
PASSWORD_HASH = "not-a-real-hash"


def _claims(**overrides) -> GoogleIdentityClaims:
    base = GoogleIdentityClaims(
        subject=SUBJECT,
        email=EMAIL,
        email_verified=True,
        full_name=NAME,
        picture=PICTURE,
    )
    return replace(base, **overrides)


class _FakeCommands:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.values else 0


class _FakeRedis:
    """Session revocation is not what these tests are about."""

    def __init__(self):
        self.client = _FakeCommands()


class _ScriptedFlows:
    """A flow store that hands back one prepared flow, then nothing.

    `spend` returning None on the second call is not laziness - it is what the
    real store does, and it keeps a test that accidentally redeems twice from
    passing.
    """

    def __init__(self, flow: OAuthFlow):
        self._flow: OAuthFlow | None = flow

    async def spend(self, *, state: str) -> OAuthFlow | None:
        flow, self._flow = self._flow, None
        return flow


class _ScriptedClient:
    async def exchange(self, *, code: str, code_verifier: str) -> str:
        return "an-id-token"


class _ScriptedVerifier:
    def __init__(self, claims: GoogleIdentityClaims):
        self.claims = claims

    async def verify(self, *, id_token: str, nonce: str) -> GoogleIdentityClaims:
        return self.claims


def _service(db_session, settings: Settings, claims: GoogleIdentityClaims, *, flow: OAuthFlow):
    return GoogleAuthService(
        session=db_session,
        settings=settings,
        flows=_ScriptedFlows(flow),
        client=_ScriptedClient(),
        verifier=_ScriptedVerifier(claims),
        auth=AuthService(
            session=db_session,
            settings=settings,
            token_store=RefreshTokenStore(_FakeRedis()),
        ),
    )


# The secret a browser would have been given at `authorize`, and the digest the
# flow record would hold. Real values rather than placeholders, because
# `_redeem` compares them and a flow built with a stand-in digest would refuse
# every login in this file.
BROWSER_SECRET = "browser-binding-secret-for-these-tests"
BROWSER_BINDING = hash_binding(BROWSER_SECRET)


def _login_flow() -> OAuthFlow:
    return OAuthFlow(
        kind=FlowKind.LOGIN,
        nonce="n",
        code_verifier="v",
        binding=BROWSER_BINDING,
    )


def _link_flow(user_id: uuid.UUID) -> OAuthFlow:
    return OAuthFlow(
        kind=FlowKind.LINK,
        nonce="n",
        code_verifier="v",
        binding=BROWSER_BINDING,
        user_id=user_id,
    )


async def _login(db_session, settings, claims: GoogleIdentityClaims, *, state: str) -> None:
    await _service(db_session, settings, claims, flow=_login_flow()).complete_login(
        code="a-code", state=state, binding=BROWSER_SECRET
    )
    await db_session.flush()


async def _stored(db_session, email: str = EMAIL) -> User:
    found = await db_session.scalar(select(User).where(User.email == email))
    assert found is not None
    return found


async def test_a_first_login_stores_the_name_and_the_picture(db_session, settings):
    """The account and its profile land in one transaction, or not at all."""
    await _login(db_session, settings, _claims(), state="s")

    user = await _stored(db_session)
    assert user.full_name == NAME
    assert user.avatar_url == PICTURE
    # The address arrived inside a signature, so it is recorded as proven.
    assert user.email_verified_at is not None
    assert user.hashed_password is None


async def test_a_later_login_follows_a_changed_google_profile(db_session, settings):
    """The reason the refresh runs on every login and not only the first."""
    await _login(db_session, settings, _claims(), state="s")
    await _login(
        db_session,
        settings,
        _claims(full_name="A New Name", picture=MOVED_PICTURE),
        state="s2",
    )

    user = await _stored(db_session)
    assert user.full_name == "A New Name"
    assert user.avatar_url == MOVED_PICTURE


async def test_a_later_login_never_moves_the_account_to_a_new_address(db_session, settings):
    """The security property, asserted against real rows.

    A Google account that later reports a different address must not drag the
    Wasla account onto it. If it could, control of the Google account would be
    the power to redirect every future password reset - and the second login
    below would quietly create exactly that situation.
    """
    await _login(db_session, settings, _claims(), state="s")
    # Read before expiring: an identity map entry whose attributes have been
    # expired cannot be dereferenced without a lazy load, and a lazy load on an
    # async session is an error rather than a query.
    original_id = (await _stored(db_session)).id

    await _login(
        db_session,
        settings,
        _claims(email="attacker-controlled@example.com", full_name="Still Followed"),
        state="s2",
    )
    db_session.expire_all()

    user = await db_session.get(User, original_id)
    assert user is not None
    assert user.email == EMAIL
    # The display claims still followed, which is what makes the address
    # standing still a decision rather than the refresh simply not running.
    assert user.full_name == "Still Followed"
    moved = await db_session.scalar(
        select(User).where(User.email == "attacker-controlled@example.com")
    )
    assert moved is None


async def test_only_one_account_exists_after_two_logins(db_session, settings):
    """The identity is resolved by subject, so a second login is not a signup."""
    await _login(db_session, settings, _claims(), state="s")
    await _login(db_session, settings, _claims(), state="s2")

    assert len((await db_session.scalars(select(User))).all()) == 1
    identities = (await db_session.scalars(select(FederatedIdentity))).all()
    assert len(identities) == 1
    assert identities[0].provider is IdentityProvider.GOOGLE
    assert identities[0].provider_subject == SUBJECT


async def test_linking_gives_a_password_account_its_first_picture(db_session, settings):
    """Usually the first moment such an account can have had one at all."""
    user = await UserRepository(db_session).create(
        email="existing@example.com",
        full_name="Chosen In Wasla",
        hashed_password=PASSWORD_HASH,
    )
    await db_session.flush()
    assert user.avatar_url is None

    linked = _claims(email="existing@example.com")
    await _service(db_session, settings, linked, flow=_link_flow(user.id)).complete_link(
        user=user, code="a-code", state="s", binding=BROWSER_SECRET
    )
    await db_session.flush()

    assert user.avatar_url == PICTURE
    assert user.full_name == NAME
    # Linking must not cost the account its password.
    assert user.hashed_password == PASSWORD_HASH


async def test_a_token_without_a_picture_leaves_a_stored_one_alone(db_session, settings):
    """Google omitting a field is not somebody clearing it."""
    await _login(db_session, settings, _claims(), state="s")
    await _login(db_session, settings, _claims(picture=None), state="s2")

    assert (await _stored(db_session)).avatar_url == PICTURE
