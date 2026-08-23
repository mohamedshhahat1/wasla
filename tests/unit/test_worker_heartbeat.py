"""The worker's liveness signal.

This exists because the worker container reported unhealthy for its entire life:
the image's health check curls the API, and the worker serves no HTTP. The
tests below pin what the replacement actually claims — which is narrower than
"the worker is working", and deliberately so.
"""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import RedisError

from app.workers.heartbeat import (
    DEFAULT_TTL_SECONDS,
    Heartbeat,
    all_alive,
    heartbeat_key,
)
from app.workers.runner import beat_while_running


class FakeCommands:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.keys: dict[str, int] = {}
        self.sets = 0

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        if self.broken:
            raise RedisError("redis is not there")
        self.sets += 1
        self.keys[key] = ex or 0
        return True

    async def exists(self, key: str) -> int:
        if self.broken:
            raise RedisError("redis is not there")
        return 1 if key in self.keys else 0


class FakeRedis:
    def __init__(self, commands: FakeCommands) -> None:
        self._commands = commands

    @property
    def client(self) -> FakeCommands:
        return self._commands


def _redis(**kwargs) -> tuple[FakeRedis, FakeCommands]:
    commands = FakeCommands(**kwargs)
    return FakeRedis(commands), commands


async def test_a_beat_expires_on_its_own():
    """Liveness is "does the key exist", so nothing has to compare timestamps
    or reason about clock skew between the container and Redis."""
    redis, commands = _redis()

    await Heartbeat(redis, kind="agent").beat()

    assert commands.keys[heartbeat_key("agent")] == DEFAULT_TTL_SECONDS


async def test_a_kind_that_has_beaten_is_alive():
    redis, _ = _redis()
    heartbeat = Heartbeat(redis, kind="agent")

    await heartbeat.beat()

    assert await heartbeat.is_alive() is True


async def test_a_kind_that_has_not_beaten_is_not_alive():
    redis, _ = _redis()

    assert await Heartbeat(redis, kind="agent").is_alive() is False


async def test_a_failed_beat_does_not_kill_the_worker():
    """A worker that crashed because it could not announce itself would turn a
    cache blip into an outage of the thing being watched."""
    redis, _ = _redis(broken=True)

    assert await Heartbeat(redis, kind="agent").beat() is False


async def test_an_unreadable_heartbeat_reports_not_alive():
    """The opposite of the rate limiter's choice, deliberately: a limiter
    failing open keeps a working system serving, while a health probe failing
    open hides exactly the outage it exists to report."""
    redis, _ = _redis(broken=True)

    assert await Heartbeat(redis, kind="agent").is_alive() is False


async def test_every_configured_loop_must_be_beating():
    """A container told to run six loops with one dead is not healthy, and
    reporting it healthy is how a stopped loop survives for weeks."""
    redis, _ = _redis()
    await Heartbeat(redis, kind="agent").beat()
    await Heartbeat(redis, kind="media").beat()

    assert await all_alive(redis, ("agent", "media")) is True
    assert await all_alive(redis, ("agent", "media", "campaign")) is False


async def test_a_worker_running_nothing_is_not_healthy():
    """An empty set of kinds is a misconfiguration, not a healthy idle."""
    redis, _ = _redis()

    assert await all_alive(redis, ()) is False


async def test_kinds_are_counted_separately():
    redis, _ = _redis()
    await Heartbeat(redis, kind="agent").beat()

    assert await Heartbeat(redis, kind="campaign").is_alive() is False


# ------------------------------------------------------------- the beat task


async def test_the_beat_task_refreshes_until_asked_to_stop():
    redis, commands = _redis()
    stopping = asyncio.Event()

    task = asyncio.create_task(
        beat_while_running(redis, ("agent", "media"), stopping=stopping, interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)
    stopping.set()
    await asyncio.wait_for(task, timeout=1)

    # Two kinds, several rounds.
    assert commands.sets >= 4
    assert set(commands.keys) == {heartbeat_key("agent"), heartbeat_key("media")}


async def test_the_beat_task_stops_promptly():
    """Shutdown must not wait out a beat interval; a deploy that takes an extra
    thirty seconds per container is a deploy people stop doing."""
    redis, _ = _redis()
    stopping = asyncio.Event()
    task = asyncio.create_task(
        beat_while_running(redis, ("agent",), stopping=stopping, interval_seconds=30)
    )
    await asyncio.sleep(0.01)

    stopping.set()

    await asyncio.wait_for(task, timeout=1)


async def test_the_beat_task_survives_a_redis_outage():
    """It keeps trying rather than dying, so the container recovers on its own
    when Redis comes back."""
    redis, _ = _redis(broken=True)
    stopping = asyncio.Event()

    task = asyncio.create_task(
        beat_while_running(redis, ("agent",), stopping=stopping, interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)
    stopping.set()

    await asyncio.wait_for(task, timeout=1)


@pytest.mark.parametrize(
    "kind", ["agent", "media", "ingestion", "follow_up", "campaign", "billing"]
)
def test_every_worker_kind_has_a_key(kind: str):
    """A kind added to the runner without a heartbeat would be a loop the
    health check silently ignores."""
    assert heartbeat_key(kind).startswith("worker:heartbeat:")
