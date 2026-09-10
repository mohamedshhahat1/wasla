"""The lifecycle end to end, over a real socket, with real logins.

The integration suites drive the application through httpx's in-process ASGI
transport, which is fast and cannot see everything: it awaits the whole
application call, dependency teardown included, before returning. That hides
ordering. The lifecycle has a genuine ordering hazard of exactly that shape - a
deletion or a suspension has to be *durable* before the response that announces
it, because the next request is often the client checking that access really
did stop, and a response sent ahead of its commit would report a state the
database had not reached. `CommittingRoute` is what prevents that, and only a
real socket shows it working.

Four journeys, each one a sequence somebody actually performs:

1. register, invite, accept, transfer ownership, and the founder leaves;
2. sign up without a workspace, create one through onboarding, use it;
3. close an account, and find every door shut behind you;
4. close a workspace, and find the unrelated one still open.

Plus the platform's own: suspend a workspace, watch its members lose access,
restore it, watch them get it back.

Everything these tests write is real and is not rolled back, so each one tidies
up after itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.session import Database
from app.main import create_app
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

API = "/api/v1"
PASSWORD = "correct horse battery staple"
# Its own logical database, so a lifecycle run cannot disturb the billing E2E's
# token store or be disturbed by it.
REDIS_URL = "redis://localhost:6379/13"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.asynccontextmanager
async def _serving(database_url: str) -> AsyncIterator[str]:
    """The real application on a loopback socket, with nothing stubbed."""
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        rate_limit_enabled=False,
        app_public_url="https://e2e.example.com",
        # No catalogue is seeded here, and creating a workspace must not depend
        # on one - the behaviour `bootstrap_default_subscription` promises.
        default_plan_code="",
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        # Registration writes an invitation-capable workspace, and the
        # credential store refuses to start without a key.
        credential_encryption_keys=[TEST_CREDENTIAL_ENCRYPTION_KEY],
    )
    app = create_app(settings)
    database = Database(settings)
    redis = RedisClient(settings)
    app.state.database = database
    app.state.redis = redis

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(600):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - the server failed to come up
            raise RuntimeError("the test server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=15)
        await database.dispose()
        await redis.close()


@pytest.fixture
def scratch_engine(prepared_database: str) -> AsyncEngine:
    """A pool the test reads and writes through, outside the server."""
    return create_async_engine(prepared_database, poolclass=NullPool)


async def _verify(engine: AsyncEngine, *email: str) -> None:
    """Mark addresses proven. Verification has its own E2E path; this is not it."""
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET email_verified_at = now() WHERE email = ANY(:emails)"),
            {"emails": list(email)},
        )


async def _grant_platform_role(engine: AsyncEngine, email: str) -> None:
    """Platform authority is granted by an operator with database access (ADR-094).

    There is no HTTP route that mints platform staff, deliberately, so the test
    reaches for the database exactly as the runbook tells an operator to.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET platform_role = 'platform_admin' WHERE email = :email"),
            {"email": email},
        )


async def _forget(engine: AsyncEngine, *, slugs: list[str], emails: list[str]) -> None:
    """Remove what the server committed, on connections it does not own."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM audit_logs WHERE tenant_id IN"
                " (SELECT id FROM tenants WHERE slug = ANY(:slugs))"
            ),
            {"slugs": slugs},
        )
        await connection.execute(
            text(
                "DELETE FROM audit_logs WHERE actor_id IN"
                " (SELECT id FROM users WHERE email = ANY(:emails))"
            ),
            {"emails": emails},
        )
        for table in ("subscriptions", "tenant_invitations", "memberships"):
            await connection.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN"  # noqa: S608 - literal table names
                    " (SELECT id FROM tenants WHERE slug = ANY(:slugs))"
                ),
                {"slugs": slugs},
            )
        await connection.execute(
            text(
                "DELETE FROM email_messages WHERE user_id IN"
                " (SELECT id FROM users WHERE email = ANY(:emails))"
            ),
            {"emails": emails},
        )
        await connection.execute(
            text(
                "DELETE FROM email_verification_challenges WHERE user_id IN"
                " (SELECT id FROM users WHERE email = ANY(:emails))"
            ),
            {"emails": emails},
        )
        await connection.execute(
            text(
                "DELETE FROM memberships WHERE user_id IN"
                " (SELECT id FROM users WHERE email = ANY(:emails))"
            ),
            {"emails": emails},
        )
        await connection.execute(
            text("DELETE FROM users WHERE email = ANY(:emails)"),
            {"emails": emails},
        )
        await connection.execute(
            text("DELETE FROM tenants WHERE slug = ANY(:slugs)"),
            {"slugs": slugs},
        )


async def _register(
    client: httpx.AsyncClient,
    *,
    email: str,
    slug: str,
) -> dict[str, Any]:
    response = await client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "workspace_name": slug.title(),
            "workspace_slug": slug,
        },
    )
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _select(client: httpx.AsyncClient, session: dict[str, Any], slug: str) -> dict[str, str]:
    response = await client.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": slug},
        headers=_bearer(session),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _invitation_token(engine: AsyncEngine, email: str) -> str:
    """The raw token the invitation email carries.

    Only the hash is stored, so the raw value cannot be read back - which is the
    design. The test therefore *replaces* the row's hash with one it knows,
    which exercises the acceptance path exactly as a real token would while
    keeping the secret out of the database's reach.
    """
    from app.core.security import generate_invitation_token

    raw, token_hash = generate_invitation_token()
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE tenant_invitations SET token_hash = :hash WHERE email = :email"),
            {"hash": token_hash, "email": email},
        )
    return raw


# ------------------------------------------------------- journey one: a team


async def test_a_founder_invites_a_colleague_hands_over_and_leaves(
    prepared_database: str,
    scratch_engine: AsyncEngine,
) -> None:
    """The whole team journey, in the order a real one happens.

    Register, invite, accept, transfer ownership, and the founder walks out. The
    last step is the one that could not be done before this change: leaving was
    refused while you were the only owner, and there was no route to stop being
    the only owner. The workspace survives its founder, with somebody who can
    still run it - which is the point of the whole ownership design.
    """
    suffix = uuid.uuid4().hex[:8]
    slug = f"e2e-team-{suffix}"
    founder = f"founder-{suffix}@example.com"
    colleague = f"colleague-{suffix}@example.com"

    async with (
        _serving(prepared_database) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=30) as client,
    ):
        try:
            await _register(client, email=founder, slug=slug)
            await _verify(scratch_engine, founder)
            founder_session = await _login(client, founder)
            headers = await _select(client, founder_session, slug)

            invited = await client.post(
                f"{API}/invitations",
                json={"email": colleague, "role": "member"},
                headers=headers,
            )
            assert invited.status_code == 201, invited.text

            token = await _invitation_token(scratch_engine, colleague)
            accepted = await client.post(
                f"{API}/invitations/accept",
                json={"token": token, "password": PASSWORD, "full_name": "A Colleague"},
            )
            assert accepted.status_code in (200, 201), accepted.text
            await _verify(scratch_engine, colleague)

            members = await client.get(f"{API}/workspace/members", headers=headers)
            assert members.status_code == 200, members.text
            colleague_id = next(
                row["user_id"] for row in members.json()["members"] if row["email"] == colleague
            )

            transferred = await client.post(
                f"{API}/workspace/ownership",
                json={"user_id": colleague_id},
                headers=headers,
            )
            assert transferred.status_code == 200, transferred.text
            assert transferred.json()["new_owner_role"] == "tenant_owner"
            assert transferred.json()["previous_owner_role"] == "tenant_admin"

            # And now the founder can do the thing that was impossible.
            me = await client.get(f"{API}/auth/me", headers=_bearer(founder_session))
            founder_id = me.json()["id"]
            left = await client.delete(
                f"{API}/workspace/members/{founder_id}",
                headers=headers,
            )
            assert left.status_code == 200, left.text

            # The workspace outlives them, run by its new owner.
            colleague_session = await _login(client, colleague)
            theirs = await _select(client, colleague_session, slug)
            assert (await client.get(f"{API}/workspace", headers=theirs)).status_code == 200

            # The founder still has an account; they simply are not here.
            profile = await client.get(f"{API}/auth/me", headers=_bearer(founder_session))
            assert profile.status_code == 200, profile.text
            assert profile.json()["workspaces"] == []
        finally:
            await _forget(scratch_engine, slugs=[slug], emails=[founder, colleague])


# ----------------------------------------------- journey two: onboarding late


async def test_an_account_with_no_workspace_onboards_into_one(
    prepared_database: str,
    scratch_engine: AsyncEngine,
) -> None:
    """The Google-first shape, driven by the routes a client would call.

    A Google sign-in produces an account with a session and no workspace. The
    account here is made the same way - through the invitation path, which
    creates one without any workspace of its own - because a genuine Google
    callback needs Google. What matters is the state: a valid session,
    `active_workspace: null`, and an empty workspace list. That was a dead end,
    and this is the way out of it.
    """
    suffix = uuid.uuid4().hex[:8]
    host_slug = f"e2e-host-{suffix}"
    own_slug = f"e2e-own-{suffix}"
    host = f"host-{suffix}@example.com"
    newcomer = f"newcomer-{suffix}@example.com"

    async with (
        _serving(prepared_database) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=30) as client,
    ):
        try:
            await _register(client, email=host, slug=host_slug)
            await _verify(scratch_engine, host)
            host_session = await _login(client, host)
            host_headers = await _select(client, host_session, host_slug)
            invited = await client.post(
                f"{API}/invitations",
                json={"email": newcomer, "role": "member"},
                headers=host_headers,
            )
            assert invited.status_code == 201, invited.text
            token = await _invitation_token(scratch_engine, newcomer)
            await client.post(
                f"{API}/invitations/accept",
                json={"token": token, "password": PASSWORD, "full_name": "A Newcomer"},
            )
            await _verify(scratch_engine, newcomer)

            # They leave, and are now exactly the shape a Google-first
            # account is: a session, and nowhere to be.
            session = await _login(client, newcomer)
            headers = await _select(client, session, host_slug)
            me = await client.get(f"{API}/auth/me", headers=_bearer(session))
            await client.delete(
                f"{API}/workspace/members/{me.json()['id']}",
                headers=headers,
            )

            session = await _login(client, newcomer)
            assert session["active_workspace"] is None
            stranded = await client.get(f"{API}/leads", headers=_bearer(session))
            assert stranded.status_code == 403, stranded.text

            created = await client.post(
                f"{API}/workspaces",
                json={"name": "Their Own Business", "slug": own_slug},
                headers=_bearer(session),
            )
            assert created.status_code == 201, created.text
            assert created.json()["role"] == "tenant_owner"

            own_headers = await _select(client, session, own_slug)
            usable = await client.get(f"{API}/workspace/members", headers=own_headers)
            assert usable.status_code == 200, usable.text
            assert [row["email"] for row in usable.json()["members"]] == [newcomer]
        finally:
            await _forget(
                scratch_engine,
                slugs=[host_slug, own_slug],
                emails=[host, newcomer],
            )


# --------------------------------------------------- journey three: goodbye


async def test_closing_an_account_shuts_every_door_over_a_real_socket(
    prepared_database: str,
    scratch_engine: AsyncEngine,
) -> None:
    """Delete the account, then try all three ways back in.

    Over a real socket the ordering is the interesting part: the response
    arrives only once the tombstone is durable, so the very next request - made
    on a connection the server has no idea about - genuinely sees the committed
    state rather than a race the in-process transport would have hidden.
    """
    suffix = uuid.uuid4().hex[:8]
    slug = f"e2e-bye-{suffix}"
    leaver = f"leaver-{suffix}@example.com"
    heir = f"heir-{suffix}@example.com"

    async with (
        _serving(prepared_database) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=30) as client,
    ):
        try:
            await _register(client, email=leaver, slug=slug)
            await _verify(scratch_engine, leaver)
            session = await _login(client, leaver)
            headers = await _select(client, session, slug)

            # Owning a workspace blocks closure until it is handed over.
            blocked = await client.request(
                "DELETE",
                f"{API}/auth/me",
                json={"current_password": PASSWORD},
                headers=_bearer(session),
            )
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["error"]["code"] == "account_owns_workspaces"
            assert [row["slug"] for row in blocked.json()["error"]["details"]["workspaces"]] == [
                slug
            ]

            invited = await client.post(
                f"{API}/invitations",
                json={"email": heir, "role": "member"},
                headers=headers,
            )
            assert invited.status_code == 201, invited.text
            token = await _invitation_token(scratch_engine, heir)
            await client.post(
                f"{API}/invitations/accept",
                json={"token": token, "password": PASSWORD, "full_name": "An Heir"},
            )
            members = await client.get(f"{API}/workspace/members", headers=headers)
            heir_id = next(
                row["user_id"] for row in members.json()["members"] if row["email"] == heir
            )
            handed = await client.post(
                f"{API}/workspace/ownership",
                json={"user_id": heir_id},
                headers=headers,
            )
            assert handed.status_code == 200, handed.text

            closed = await client.request(
                "DELETE",
                f"{API}/auth/me",
                json={"current_password": PASSWORD},
                headers=_bearer(session),
            )
            assert closed.status_code == 200, closed.text

            # Door one: the access token that made the call.
            assert (await client.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 401
            # Door two: the refresh token.
            assert (
                await client.post(
                    f"{API}/auth/refresh",
                    json={"refresh_token": session["refresh_token"]},
                )
            ).status_code == 401
            # Door three: the credentials themselves.
            assert (
                await client.post(
                    f"{API}/auth/login",
                    json={"email": leaver, "password": PASSWORD},
                )
            ).status_code == 401

            # The colleague is entirely unaffected.
            heir_session = await _login(client, heir)
            await _verify(scratch_engine, heir)
            heir_session = await _login(client, heir)
            theirs = await _select(client, heir_session, slug)
            assert (await client.get(f"{API}/workspace", headers=theirs)).status_code == 200
        finally:
            await _forget(scratch_engine, slugs=[slug], emails=[leaver, heir])


# ------------------------------------------- journey four: closing a business


async def test_closing_one_workspace_leaves_an_unrelated_one_open(
    prepared_database: str,
    scratch_engine: AsyncEngine,
) -> None:
    """Two workspaces, one owner, one deletion.

    The assertion that matters is the negative one: the second workspace is
    untouched and the account is untouched. Deleting a tenant must never reach
    a global identity or another tenant, and this is that stated as a journey
    rather than as a unit test.
    """
    suffix = uuid.uuid4().hex[:8]
    doomed = f"e2e-doomed-{suffix}"
    kept = f"e2e-kept-{suffix}"
    owner = f"twohats-{suffix}@example.com"

    async with (
        _serving(prepared_database) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=30) as client,
    ):
        try:
            await _register(client, email=owner, slug=doomed)
            await _verify(scratch_engine, owner)
            session = await _login(client, owner)
            second = await client.post(
                f"{API}/workspaces",
                json={"name": "Kept", "slug": kept},
                headers=_bearer(session),
            )
            assert second.status_code == 201, second.text

            doomed_headers = await _select(client, session, doomed)
            wrong = await client.request(
                "DELETE",
                f"{API}/workspace",
                json={"confirmation": kept},
                headers=doomed_headers,
            )
            assert wrong.status_code == 409, wrong.text
            assert wrong.json()["error"]["code"] == "workspace_confirmation_invalid"

            closed = await client.request(
                "DELETE",
                f"{API}/workspace",
                json={"confirmation": doomed},
                headers=doomed_headers,
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["is_active"] is False

            # The token that just closed it opens nothing here any more.
            assert (
                await client.get(f"{API}/workspace/members", headers=doomed_headers)
            ).status_code == 404

            # The other business carries on, and the account is intact.
            kept_headers = await _select(client, session, kept)
            assert (await client.get(f"{API}/workspace", headers=kept_headers)).status_code == 200
            profile = await client.get(f"{API}/auth/me", headers=_bearer(session))
            assert profile.status_code == 200, profile.text
            assert [row["slug"] for row in profile.json()["workspaces"]] == [kept]
        finally:
            await _forget(scratch_engine, slugs=[doomed, kept], emails=[owner])


# ---------------------------------------------- the platform's own lifecycle


async def test_platform_staff_suspend_a_workspace_and_restore_it(
    prepared_database: str,
    scratch_engine: AsyncEngine,
) -> None:
    """Service stops for the customer, then starts again, and nothing else moves.

    The restore is checked as carefully as the suspend: it must give back
    exactly what suspension took and nothing more. A lifecycle operation that
    quietly restored access somebody had lost for a different reason would be a
    security defect wearing the shape of a convenience.
    """
    suffix = uuid.uuid4().hex[:8]
    slug = f"e2e-susp-{suffix}"
    owner = f"customer-{suffix}@example.com"
    staff = f"staff-{suffix}@example.com"
    staff_slug = f"e2e-staff-{suffix}"

    async with (
        _serving(prepared_database) as base_url,
        httpx.AsyncClient(base_url=base_url, timeout=30) as client,
    ):
        try:
            await _register(client, email=owner, slug=slug)
            await _register(client, email=staff, slug=staff_slug)
            await _verify(scratch_engine, owner, staff)
            await _grant_platform_role(scratch_engine, staff)

            owner_session = await _login(client, owner)
            owner_headers = await _select(client, owner_session, slug)
            assert (await client.get(f"{API}/workspace", headers=owner_headers)).status_code == 200

            staff_session = await _login(client, staff)
            listed = await client.get(
                f"{API}/platform/tenants",
                headers=_bearer(staff_session),
            )
            assert listed.status_code == 200, listed.text
            # `items`, and found by slug rather than by position: the
            # listing is sorted by name and this test must not depend on
            # where a randomly suffixed workspace happens to land.
            tenant_id = next(
                row["tenant"]["id"]
                for row in listed.json()["items"]
                if row["tenant"]["slug"] == slug
            )

            suspended = await client.post(
                f"{API}/platform/tenants/{tenant_id}/suspend",
                json={"reason": "billing dispute"},
                headers=_bearer(staff_session),
            )
            assert suspended.status_code == 200, suspended.text
            assert suspended.json()["status"] == "suspended"

            # The customer's existing token stops working at once.
            stopped = await client.get(f"{API}/workspace", headers=owner_headers)
            assert stopped.status_code == 403, stopped.text
            # Their account still works; only this workspace is stopped.
            assert (
                await client.get(f"{API}/auth/me", headers=_bearer(owner_session))
            ).status_code == 200

            restored = await client.post(
                f"{API}/platform/tenants/{tenant_id}/restore",
                headers=_bearer(staff_session),
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["status"] == "active"

            back = await client.get(f"{API}/workspace", headers=owner_headers)
            assert back.status_code == 200, back.text
        finally:
            await _forget(
                scratch_engine,
                slugs=[slug, staff_slug],
                emails=[owner, staff],
            )
