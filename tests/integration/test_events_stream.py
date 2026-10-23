"""The inbox's event stream, end to end over a real server (RT-01, RT-02, RT-05, RT-06).

A real uvicorn serving the real application in this process, a real Redis
(this stage's own, database 15) and the real ingestion path; the client reads
`text/event-stream` with httpx line by line, as a browser's EventSource would.
httpx's ASGI transport is not used: it buffers a response to its end, and a
stream has none.

Keep-alives are one second here (`RT_KEEPALIVE_SECONDS`), so every check a
stream makes "at least every keep-alive" is observed within seconds; the
request timeout is two seconds, so a stream held for five proves the stream
route is exempt from it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.core import events
from app.core.config import Settings
from app.core.security import create_access_token
from app.db.models.enums import MembershipStatus, TenantRole, TenantStatus
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import create_app
from app.services.realtime import issue_ticket
from tests.integration.campaign_harness import PLATFORM_TOKEN, FakeMeta, erase, worker
from tests.integration.sequence_harness import World, ai_reply, build, conversations
from tests.integration.sequence_harness import customer_writes as _customer_writes

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

TICKET = "/api/v1/events/ticket"
STREAM = "/api/v1/events/stream"
KEEPALIVE = 1.0

#: Every event any test of this file received: scanned for content at the end of each.
SEEN: list[dict[str, Any]] = []
FORBIDDEN_KEYS = {"body", "text", "name", "phone", "wa_id", "media_id", "parameters"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Live:
    mine: World
    theirs: World
    settings: Settings
    base_url: str
    server: uvicorn.Server
    redis: Redis
    words: list[str] = field(default_factory=list)


@pytest_asyncio.fixture
async def live(prepared_database: str) -> AsyncIterator[Live]:
    url = os.environ.get("WASLA_TEST_REDIS_URL")
    if not url:
        pytest.skip("No test Redis: set WASLA_TEST_REDIS_URL.")
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-stream")
    configured = Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        database_url=prepared_database,
        redis_url=url,
        meta_access_token=PLATFORM_TOKEN,
        request_timeout_seconds=2.0,
        rt_keepalive_seconds=KEEPALIVE,
    )
    mine = await build(database, prepared_database, meta, steps=[5], recipients=2, enabled=False)
    theirs = await build(database, prepared_database, meta, steps=[5], recipients=1, enabled=False)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(configured), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            serving.result()
        await asyncio.sleep(0.05)
    redis = Redis.from_url(url, decode_responses=True)
    state = Live(mine, theirs, configured, f"http://127.0.0.1:{port}", server, redis)
    try:
        yield state
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=30)
        for world in (mine, theirs):
            await redis.delete(events.stream_key(world.workspace.tenant_id))
        await redis.aclose()
        await erase(database)
        await database.dispose()
        scan(SEEN, state.words)
        SEEN.clear()


def scan(found: list[dict[str, Any]], words: list[str]) -> None:
    """No event carries a message's words, a name, a phone number or a media id (RT-I01)."""
    for hint in found:
        data = hint.get("data") or {}
        assert not FORBIDDEN_KEYS & set(data), hint
        blob = json.dumps(hint)
        for word in words:
            assert word not in blob, (word, hint)


async def customer_writes(live: Live, world: World, conversation: Any, text: str) -> None:
    live.words.append(text)
    await _customer_writes(world, conversation, text)


# ------------------------------------------------------------------ people and tickets


async def member(world: World, live: Live) -> tuple[uuid.UUID, dict[str, str]]:
    async with world.database.session_factory() as session:
        user = User(
            email=f"rt-{uuid.uuid4().hex[:8]}@wasla.test",
            hashed_password="x",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                tenant_id=world.workspace.tenant_id,
                user_id=user.id,
                role=TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            )
        )
        await session.commit()
        return user.id, headers_for(live, user.id, world, user.token_version)


def headers_for(live: Live, user_id: uuid.UUID, world: World, version: int) -> dict[str, str]:
    token, _ = create_access_token(
        settings=live.settings,
        subject=user_id,
        tenant_id=world.workspace.tenant_id,
        token_version=version,
    )
    return {"Authorization": f"Bearer {token}"}


async def ticket(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(TICKET, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"ticket", "expires_at"}
    return str(body["ticket"])


# ------------------------------------------------------------------ reading a stream


class Reader:
    """A text/event-stream read the way EventSource reads it: fields until a blank line."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._lines = response.aiter_lines()

    async def next(self, within: float = 10.0) -> dict[str, Any]:
        event: dict[str, Any] = {}
        async with asyncio.timeout(within):
            async for line in self._lines:
                if line == "":
                    if event:
                        if "data" in event:
                            event["data"] = json.loads(event["data"])
                        SEEN.append(event)
                        return event
                    continue
                if line.startswith(":"):
                    event = {"comment": line[1:].strip()}
                    SEEN.append(event)
                    return event
                name, _, value = line.partition(":")
                event[name] = value.removeprefix(" ")
        raise EOFError("the stream ended")

    async def until(self, kind: str, within: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + within
        while True:
            event = await self.next(within=max(0.1, deadline - time.monotonic()))
            if event.get("event") == kind:
                return event


@asynccontextmanager
async def stream(
    client: httpx.AsyncClient, token: str, *, last_event_id: str | None = None
) -> AsyncIterator[Reader]:
    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    async with client.stream("GET", STREAM, params={"ticket": token}, headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        reader = Reader(response)
        assert (await reader.next())["retry"] == "3000"
        yield reader


def client_for(live: Live) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=live.base_url, timeout=httpx.Timeout(30.0, read=None))


# ------------------------------------------------------------------ tickets (RT-01)


async def test_a_ticket_opens_one_stream_once(live: Live) -> None:
    user_id, headers = await member(live.mine, live)
    async with client_for(live) as client:
        token = await ticket(client, headers)
        async with stream(client, token):
            pass
        assert (await client.get(STREAM, params={"ticket": token})).status_code == 401, "reused"
        unknown = await client.get(STREAM, params={"ticket": "not-a-ticket"})
        assert unknown.status_code == 401
        assert unknown.json()["error"]["code"]

        expired, _ = await issue_ticket(
            live.redis,
            user_id=user_id,
            tenant_id=live.mine.workspace.tenant_id,
            token_version=0,
            ttl=1,
        )
        await asyncio.sleep(1.5)
        assert (await client.get(STREAM, params={"ticket": expired})).status_code == 401

        stale = await ticket(client, headers)
        async with live.mine.database.session_factory() as session:
            await session.execute(
                update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
            )
            await session.commit()
        assert (await client.get(STREAM, params={"ticket": stale})).status_code == 401, "logout"

        _, other_headers = await member(live.mine, live)
        revoked_ticket = await ticket(client, other_headers)
        async with live.mine.database.session_factory() as session:
            await session.execute(
                update(Membership)
                .where(Membership.tenant_id == live.mine.workspace.tenant_id)
                .where(Membership.user_id != user_id)
                .values(status=MembershipStatus.REVOKED)
            )
            await session.commit()
        assert (await client.get(STREAM, params={"ticket": revoked_ticket})).status_code == 401
        assert (await client.post(TICKET)).status_code == 401, "no ticket without a session"


async def test_the_ticket_is_never_in_a_log_line(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    _, headers = await member(live.mine, live)
    async with client_for(live) as client:
        token = await ticket(client, headers)
        async with stream(client, token):
            pass
        await client.get(STREAM, params={"ticket": token})
    assert token not in caplog.text


# ------------------------------------------------------------------ events (RT-02, RT-03)


async def test_a_new_message_reaches_the_stream_within_a_second(live: Live) -> None:
    _, headers = await member(live.mine, live)
    conversation = (await conversations(live.mine))[0]
    latencies: list[float] = []
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        for index in range(100):
            started = time.monotonic()
            await customer_writes(live, live.mine, conversation, f"private words {index} 7781")
            event = await reader.until("message.created", within=5)
            latencies.append(time.monotonic() - started)
            assert event["data"]["conversation_id"] == str(conversation.id)
            assert event["data"]["direction"] == "inbound"
            assert event["data"]["channel_type"] == "whatsapp"
            assert event["id"]
    latencies.sort()
    p95 = latencies[94]
    _record("p95_delivery_one_stream_seconds", p95)
    assert p95 < 1.0, latencies


async def test_an_event_right_after_the_stream_opens_is_never_missed(live: Live) -> None:
    _, headers = await member(live.mine, live)
    conversation = (await conversations(live.mine))[0]
    async with client_for(live) as client:
        for attempt in range(10):
            async with stream(client, await ticket(client, headers)) as reader:
                await customer_writes(live, live.mine, conversation, f"at once {attempt}")
                event = await reader.until("message.created", within=5)
                assert event["data"]["conversation_id"] == str(conversation.id)


async def test_a_stream_carries_only_its_own_workspace(live: Live) -> None:
    _, headers = await member(live.mine, live)
    mine = (await conversations(live.mine))[0]
    theirs = (await conversations(live.theirs))[0]
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        await customer_writes(live, live.theirs, theirs, "their secret 5512")
        await ai_reply(live.theirs, theirs, "their answer")
        await customer_writes(live, live.mine, mine, "my words")
        received = []
        while True:
            event = await reader.next(within=5)
            if "event" in event:
                received.append(event)
                if event["event"] == "message.created":
                    break
    assert received, "the stream saw its own workspace's message"
    for event in received:
        assert event["data"].get("conversation_id") != str(theirs.id), event


async def test_last_event_id_resumes_without_a_gap_or_a_duplicate(live: Live) -> None:
    _, headers = await member(live.mine, live)
    conversation = (await conversations(live.mine))[0]
    async with client_for(live) as client:
        async with stream(client, await ticket(client, headers)) as reader:
            await customer_writes(live, live.mine, conversation, "first")
            first = await reader.until("message.created")
        for text in ("second", "third"):
            await customer_writes(live, live.mine, conversation, text)
        await events.drain()
        missed = [
            entry_id
            for entry_id, fields in await live.redis.xrange(
                events.stream_key(live.mine.workspace.tenant_id), min=f"({first['id']}"
            )
        ]
        assert len(missed) >= 2
        async with stream(client, await ticket(client, headers), last_event_id=first["id"]) as rd:
            replayed = [(await rd.next())["id"] for _ in missed]
            assert replayed == missed, "every missed event, in order, none twice"
            await customer_writes(live, live.mine, conversation, "fourth")
            live_event = await rd.until("message.created")
            assert live_event["id"] not in replayed


async def test_a_last_event_id_older_than_the_stream_starts_with_resync(live: Live) -> None:
    _, headers = await member(live.mine, live)
    conversation = (await conversations(live.mine))[0]
    key = events.stream_key(live.mine.workspace.tenant_id)
    async with client_for(live) as client:
        async with stream(client, await ticket(client, headers)) as reader:
            await customer_writes(live, live.mine, conversation, "old")
            old = await reader.until("message.created")
        await customer_writes(live, live.mine, conversation, "new")
        await events.drain()
        newest = (await live.redis.xrevrange(key, count=1))[0][0]
        # Retention has passed the client's id.
        await live.redis.xtrim(key, minid=newest, approximate=False)
        async with stream(client, await ticket(client, headers), last_event_id=old["id"]) as rd:
            assert (await rd.next())["event"] == "resync"


# ------------------------------------------------------------------ lifetime (RT-05)


async def _closed_reason(reader: Reader) -> str:
    event = await reader.until("closed", within=KEEPALIVE * 5)
    with pytest.raises(EOFError):
        await reader.next(within=KEEPALIVE * 5)
    return str(event["data"]["reason"])


async def test_revoking_the_membership_closes_the_stream(live: Live) -> None:
    user_id, headers = await member(live.mine, live)
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        async with live.mine.database.session_factory() as session:
            await session.execute(
                update(Membership)
                .where(Membership.user_id == user_id)
                .values(status=MembershipStatus.REVOKED)
            )
            await session.commit()
        assert await _closed_reason(reader) == "membership_revoked"


async def test_logging_out_closes_the_stream(live: Live) -> None:
    user_id, headers = await member(live.mine, live)
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        async with live.mine.database.session_factory() as session:
            await session.execute(
                update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
            )
            await session.commit()
        assert await _closed_reason(reader) == "token_changed"


async def test_switching_workspace_closes_the_other_stream(live: Live) -> None:
    user_id, headers = await member(live.mine, live)
    async with live.theirs.database.session_factory() as session:
        session.add(
            Membership(
                tenant_id=live.theirs.workspace.tenant_id,
                user_id=user_id,
                role=TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            )
        )
        await session.commit()
    async with live.theirs.database.session_factory() as session:
        version = await session.scalar(select(User.token_version).where(User.id == user_id))
    assert version is not None
    theirs = headers_for(live, user_id, live.theirs, version)
    async with (
        client_for(live) as client,
        stream(client, await ticket(client, headers)) as reader,
        stream(client, await ticket(client, theirs)),
    ):
        assert await _closed_reason(reader) == "workspace_switched"


async def test_suspending_the_workspace_closes_the_stream(live: Live) -> None:
    _, headers = await member(live.mine, live)
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        async with live.mine.database.session_factory() as session:
            await session.execute(
                update(Tenant)
                .where(Tenant.id == live.mine.workspace.tenant_id)
                .values(status=TenantStatus.SUSPENDED)
            )
            await session.commit()
        assert await _closed_reason(reader) == "workspace_unavailable"


async def test_a_sixth_stream_for_one_person_is_refused(live: Live) -> None:
    _, headers = await member(live.mine, live)
    async with client_for(live) as client:
        opened = [stream(client, await ticket(client, headers)) for _ in range(5)]
        readers = [await context.__aenter__() for context in opened]
        try:
            sixth = await client.get(STREAM, params={"ticket": await ticket(client, headers)})
            assert sixth.status_code == 429
            assert sixth.json()["error"]["code"] == "stream_limit"
        finally:
            for context in reversed(opened):
                await context.__aexit__(None, None, None)
        assert len(readers) == 5
        async with stream(client, await ticket(client, headers)):
            pass  # a closed stream frees its place at once


async def test_a_stream_outlives_the_request_timeout_and_keeps_alive(live: Live) -> None:
    _, headers = await member(live.mine, live)
    conversation = (await conversations(live.mine))[0]
    async with client_for(live) as client, stream(client, await ticket(client, headers)) as reader:
        started = time.monotonic()
        comments = 0
        while time.monotonic() - started < live.settings.request_timeout_seconds * 2.5:
            event = await reader.next(within=KEEPALIVE * 3)
            comments += event.get("comment") == "keep-alive"
        assert comments >= 3
        await customer_writes(live, live.mine, conversation, "still here")
        assert (await reader.until("message.created"))["data"]["conversation_id"]


async def test_a_graceful_shutdown_closes_every_stream_first(live: Live) -> None:
    _, headers = await member(live.mine, live)
    async with client_for(live) as client:
        contexts = [stream(client, await ticket(client, headers)) for _ in range(3)]
        readers = [await context.__aenter__() for context in contexts]
        try:
            assert getattr(uvicorn.Server.handle_exit, "_closes_streams", False), "hook installed"
            # What SIGTERM runs. Uvicorn then re-raises the signal it captured
            # once it has stopped (to end the process) - here that would end
            # the test session, so the captured signal is dropped.
            live.server.handle_exit(signal.SIGTERM, None)
            live.server._captured_signals.clear()
            reasons = [await _closed_reason(reader) for reader in readers]
        finally:
            for context in reversed(contexts):
                await context.__aexit__(None, None, None)
    assert reasons == ["server_restart"] * 3


def _record(name: str, value: float) -> None:
    """Numbers the report quotes (section 30), when a run asks for them."""
    path = os.environ.get("WASLA_RT_NUMBERS_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "value": value}) + "\n")
