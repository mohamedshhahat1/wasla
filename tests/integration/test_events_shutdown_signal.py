"""A real signal to a real API process closes every open stream first (FRM-03, RT-06).

`test_events_stream.py` serves the application in this process and calls the
exit handler itself; it cannot see which handler the operating system runs.
Here the API runs as it ships - `scripts/entrypoint.sh api`, which execs
`python -m app.serve` - in a child process on a free port, against this
session's database and Redis, and the test sends it SIGTERM or SIGINT with
`os.kill`. Uvicorn registers its handler before it imports the application,
so a hook the application installs while it is imported would never run:
this is the test that tells the two apart.

POSIX only: on Windows `Popen.send_signal(SIGTERM)` is `TerminateProcess`,
which no handler sees. It runs on Linux CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import httpx
import pytest
from redis.asyncio import Redis

from app.core.telemetry import COUNTER_PREFIX
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.user import User
from app.db.session import Database
from app.services.realtime import issue_ticket
from tests.integration.campaign_harness import USER_DOMAIN, FakeMeta, erase, worker
from tests.integration.sequence_harness import World, build
from tests.redis_url import shared_redis_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="the OS signal path: SIGTERM is TerminateProcess on Windows; runs on Linux CI",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
STREAM = "/api/v1/events/stream"
CLOSED_KEY = f"{COUNTER_PREFIX}:wasla_events_stream_closed_total"
#: Start-up imports the whole application; a loaded runner can take a while.
LIVE_WITHIN = 150.0
#: A hang must fail the test, never hold the run (pytest-timeout is not in the lock).
TEST_WITHIN = 240.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Child:
    process: subprocess.Popen[bytes]
    port: int
    output: IO[bytes]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log(self) -> str:
        self.output.flush()
        return Path(self.output.name).read_text(encoding="utf-8", errors="replace")


def _environment(database_url: str, redis_url: str, port: int) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        ENVIRONMENT="test",
        JWT_SECRET=os.environ.get("JWT_SECRET", "signal-test-secret-value-not-for-deployment"),
        DATABASE_URL=database_url,
        REDIS_URL=redis_url,
        LOG_FORMAT="json",
        LOG_LEVEL="INFO",
        RATE_LIMIT_ENABLED="false",
        RT_KEEPALIVE_SECONDS="1",
        HOST="127.0.0.1",
        PORT=str(port),
        UVICORN_GRACEFUL_SHUTDOWN_SECONDS="25",
        RUN_MIGRATIONS="false",
        UVICORN_RELOAD="false",
        PYTHONPATH=str(ROOT),
        PYTHONUNBUFFERED="1",
        # `python` in the entrypoint is this interpreter.
        PATH=os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    )
    return environment


def _spawn(database_url: str, redis_url: str, command: list[str]) -> Child:
    """Start `command` on a free port, its output to a file (a pipe left unread would block it)."""
    port = _free_port()
    environment = _environment(database_url, redis_url, port)
    # The child's interpreter, environment and directory import this worktree's app.
    where = subprocess.run(  # noqa: S603 - this interpreter and a literal command
        [sys.executable, "-c", "import app; print(app.__file__)"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(where.stdout.strip()) == ROOT / "app" / "__init__.py", where.stdout
    output = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed when the child is reaped
        prefix="wasla-signal-", suffix=".log", delete=False
    )
    process = subprocess.Popen(  # noqa: S603 - a fixed command line
        [part.replace("{port}", str(port)) for part in command],
        cwd=ROOT,
        env=environment,
        stdout=output,
        stderr=subprocess.STDOUT,
    )
    return Child(process, port, output)


@asynccontextmanager
async def serving(database_url: str, redis_url: str, command: list[str]) -> AsyncIterator[Child]:
    """`command` in a child process until /health/live answers; always reaped."""
    child = _spawn(database_url, redis_url, command)
    process = child.process
    try:
        deadline = time.monotonic() + LIVE_WITHIN
        async with httpx.AsyncClient(base_url=child.base_url, timeout=2.0) as probe:
            while True:
                assert process.poll() is None, f"the API exited at start:\n{child.log()[-3000:]}"
                try:
                    if (await probe.get("/health/live")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, f"not live:\n{child.log()[-3000:]}"
                await asyncio.sleep(0.25)
        yield child
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
        child.output.close()
        os.unlink(child.output.name)


async def _workspace(prepared_database: str) -> tuple[World, User, Database]:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="rt-signal")
    world = await build(database, prepared_database, meta, steps=[5], recipients=1, enabled=False)
    async with database.session_factory() as session:
        user = User(
            email=f"rt-signal-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
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
        return world, user, database


async def _closed_count(redis: Redis) -> int:
    found = await cast("Any", redis.hgetall(CLOSED_KEY))
    return sum(int(value) for field, value in found.items() if "server_restart" in str(field))


async def _read_until_closed(
    lines: AsyncIterator[str], signalled: list[float]
) -> tuple[str, float]:
    """The `closed` event's data and the seconds after the signal it arrived; ("", t) if cut."""
    event = None
    try:
        async for line in lines:
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:") and event == "closed":
                return line.partition(":")[2].strip(), time.monotonic() - signalled[0]
    except httpx.HTTPError:
        pass
    return "", time.monotonic() - signalled[0]


def _record(case: str, first: float, last: float, exited: float) -> None:
    """Numbers the report quotes, when a run asks for them (as test_events_stream does)."""
    path = os.environ.get("WASLA_RT_NUMBERS_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            numbers = {"first_closed": first, "last_closed": last, "exited": exited}
            handle.write(json.dumps({"name": f"signal {case}", **numbers}) + "\n")


def _entrypoint() -> list[str]:
    return ["sh", str(ROOT / "scripts" / "entrypoint.sh"), "api"]


@pytest.mark.parametrize("streams", [1, 5])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
async def test_the_exit_signal_closes_every_stream_first(
    prepared_database: str, sig: signal.Signals, streams: int
) -> None:
    redis_url = shared_redis_url()
    async with asyncio.timeout(TEST_WITHIN):
        await _signal_run(prepared_database, redis_url, sig, streams)


async def _signal_run(
    prepared_database: str, redis_url: str, sig: signal.Signals, streams: int
) -> None:
    world, user, database = await _workspace(prepared_database)
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        tickets = [
            (
                await issue_ticket(
                    redis,
                    user_id=user.id,
                    tenant_id=world.workspace.tenant_id,
                    token_version=user.token_version,
                    ttl=120,
                )
            )[0]
            for _ in range(streams + 1)
        ]
        late = tickets.pop()
        before = await _closed_count(redis)
        async with (
            serving(prepared_database, redis_url, _entrypoint()) as child,
            httpx.AsyncClient(
                base_url=child.base_url, timeout=httpx.Timeout(10.0, read=None)
            ) as client,
        ):
            signalled: list[float] = []
            readers = []
            for ticket in tickets:
                response = await client.send(
                    client.build_request("GET", STREAM, params={"ticket": ticket}), stream=True
                )
                assert response.status_code == 200, await response.aread()
                readers.append((response, response.aiter_lines()))
            # The first keep-alive: every stream is being served, not only accepted.
            for _, lines in readers:
                async with asyncio.timeout(10):
                    async for line in lines:
                        if line.startswith(": keep-alive"):
                            break
            signalled.append(time.monotonic())
            os.kill(child.process.pid, sig)
            closed = await asyncio.gather(
                *(_read_until_closed(lines, signalled) for _, lines in readers)
            )
            # A stream opened after the signal is refused or closed at once.
            late_closed, late_served = True, False
            try:
                async with client.stream("GET", STREAM, params={"ticket": late}) as response:
                    if response.status_code == 200:
                        late_served = True
                        late_lines = response.aiter_lines()
                        late_closed = bool((await _read_until_closed(late_lines, signalled))[0])
            except httpx.HTTPError:
                pass
            for response, _ in readers:
                await response.aclose()
            try:
                code = await asyncio.to_thread(child.process.wait, 15)
            except subprocess.TimeoutExpired:
                code = None
            exited = time.monotonic() - signalled[0]
            log = child.log()

        assert [json.loads(data or "null") for data, _ in closed] == [
            {"reason": "server_restart"}
        ] * streams, (
            closed,
            code,
            exited,
            log[-3000:],
        )
        seconds = sorted(after for _, after in closed)
        _record(f"{sig.name}-{streams}", seconds[0], seconds[-1], exited)
        assert seconds[0] <= 2.0, seconds
        assert seconds[-1] <= 5.0, seconds
        assert late_closed, "a stream opened after the signal was held"
        assert code == 0, (code, log[-3000:])
        assert exited <= 10.0, exited
        assert "timeout graceful shutdown exceeded" not in log
        assert "running task(s)" not in log
        assert '"message": "app.shutdown"' in log
        assert "events.shutdown_hook active=true" in log
        # Every stream open at the signal, and the late one if it was served.
        assert await _closed_count(redis) - before == streams + late_served
    finally:
        await redis.aclose()
        await erase(database)
        await database.dispose()


async def test_plain_uvicorn_says_streams_will_be_cut(prepared_database: str) -> None:
    """Plain `uvicorn app.main:app` serves, and says at start-up that streams are cut (SD-04)."""
    redis_url = shared_redis_url()
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--port", "{port}"]
    async with (
        asyncio.timeout(TEST_WITHIN),
        serving(prepared_database, redis_url, command) as child,
    ):
        log = child.log()
        os.kill(child.process.pid, signal.SIGTERM)
        await asyncio.to_thread(child.process.wait, 30)
    assert "events.shutdown_hook active=false" in log
    assert "events.shutdown_hook active=true" not in log
