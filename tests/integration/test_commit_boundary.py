"""A response is never sent before the write behind it has committed.

**This file needs a real socket, and that is the whole point.** Every other
integration test drives the application through httpx's in-process ASGI
transport, which awaits the entire application call - dependency teardown
included - before handing back a response object. There is no ordering to
observe there, so the defect below is invisible to the rest of the suite by
construction. It was found by driving a container over HTTP.

The defect: the session used to commit in a `yield` dependency's teardown, which
runs *after* the response has reached the client. With a commit costing a
network round trip and an fsync that gap measured 25-75 ms against a
containerised PostgreSQL, and during it a token minted by `POST /auth/register`
was refused by `GET /auth/me` - the user row was not yet visible.

The timing was the symptom. The defect was that the API answered `201 Created`
before the write was durable, so a commit failing afterwards would leave the
caller holding a success for something that never happened. See
`app/api/route.py`.

These tests run the **real application** rather than a model of it, because the
thing being checked is the wiring: that every router really does carry the route
class, and that the session dependency really does park its session where the
route class looks for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import AsyncIterator

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

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
# A database of its own, so a run cannot disturb whatever else uses this Redis.
REDIS_URL = "redis://localhost:6379/14"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.asynccontextmanager
async def _serving(database_url: str) -> AsyncIterator[str]:
    """The real application on a real loopback socket.

    Infrastructure is attached by hand rather than through the lifespan, so the
    test owns the engine and can dispose of it deterministically.
    """
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        rate_limit_enabled=False,
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


async def _register(client: httpx.AsyncClient, *, email: str, slug: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "workspace_name": f"Boundary {slug}",
            "workspace_slug": slug,
        },
    )


async def _forget(engine: AsyncEngine, slug: str, email: str) -> None:
    """Remove what the test committed.

    It cannot use the rolled-back `db_session` fixture - the server writes on
    its own connections and those writes are real - so it tidies up itself.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM subscriptions WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = :slug)"
            ),
            {"slug": slug},
        )
        await connection.execute(
            text("DELETE FROM audit_logs WHERE actor_label = :email"),
            {"email": email},
        )
        await connection.execute(
            text(
                "DELETE FROM memberships WHERE user_id IN "
                "(SELECT id FROM users WHERE email = :email)"
            ),
            {"email": email},
        )
        await connection.execute(
            text("DELETE FROM users WHERE email = :email"),
            {"email": email},
        )
        await connection.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})


@pytest.fixture
def scratch_engine(prepared_database: str) -> AsyncEngine:
    """A pool the test reads the database through, outside the server."""
    return create_async_engine(prepared_database, poolclass=NullPool)


async def test_a_token_from_register_works_on_the_very_first_request(
    prepared_database,
    scratch_engine,
):
    """The regression, stated as a customer would experience it.

    One attempt, no retry, no sleep. This failed for 25-75 ms before the commit
    moved inside the handler chain - long enough for a client to make its next
    call and be told its brand-new credential is invalid.
    """
    stamp = uuid.uuid4().hex[:10]
    email = f"boundary-{stamp}@example.com"
    slug = f"boundary-{stamp}"
    try:
        async with (
            _serving(prepared_database) as base,
            httpx.AsyncClient(base_url=base, timeout=30) as client,
        ):
            registered = await _register(client, email=email, slug=slug)
            assert registered.status_code == 201
            token = registered.json()["access_token"]

            profile = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )

            assert (
                profile.status_code == 200
            ), "the session was handed out before its account was committed"
            assert profile.json()["email"] == email
    finally:
        await _forget(scratch_engine, slug, email)
        await scratch_engine.dispose()


async def test_the_row_exists_the_moment_the_response_lands(
    prepared_database,
    scratch_engine,
):
    """The same property, read straight from the database.

    Stronger than the test above, which could in principle be satisfied by
    something other than a commit. This one asks PostgreSQL, on a different
    connection, at the instant the client has the response.
    """
    stamp = uuid.uuid4().hex[:10]
    email = f"boundary-{stamp}@example.com"
    slug = f"boundary-{stamp}"
    try:
        async with (
            _serving(prepared_database) as base,
            httpx.AsyncClient(base_url=base, timeout=30) as client,
        ):
            registered = await _register(client, email=email, slug=slug)
            assert registered.status_code == 201

            async with scratch_engine.connect() as connection:
                found = await connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": email},
                )
                assert (
                    found.scalar_one() == 1
                ), "the response arrived before the write was committed"
    finally:
        await _forget(scratch_engine, slug, email)
        await scratch_engine.dispose()


async def test_a_failed_request_commits_nothing(prepared_database, scratch_engine):
    """The other half. Moving the commit must not weaken the rollback.

    The second registration fails on the duplicate slug *after* its user row has
    been staged, so if anything committed early the account would outlive a
    request that returned an error.
    """
    stamp = uuid.uuid4().hex[:10]
    email = f"boundary-{stamp}@example.com"
    second_email = f"boundary-second-{stamp}@example.com"
    slug = f"boundary-{stamp}"
    try:
        async with (
            _serving(prepared_database) as base,
            httpx.AsyncClient(base_url=base, timeout=30) as client,
        ):
            first = await _register(client, email=email, slug=slug)
            assert first.status_code == 201

            clash = await _register(client, email=second_email, slug=slug)
            assert clash.status_code >= 400

            async with scratch_engine.connect() as connection:
                survivors = await connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": second_email},
                )
                assert survivors.scalar_one() == 0, "a failed registration left an account behind"
    finally:
        await _forget(scratch_engine, slug, email)
        await _forget(scratch_engine, f"{slug}-absent", second_email)
        await scratch_engine.dispose()
