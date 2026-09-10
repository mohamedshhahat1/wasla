"""Single use, against a Redis that can actually interleave two callers.

Both stores in this file spend a credential in *one* Redis operation, and the
argument for doing so is written down at length in `app/core/token_store.py`
and `app/core/oauth_flow.py`: a read followed by a write is a race whose losing
branch is a replay accepted. `RefreshTokenStore.spend` is a single `SET NX`;
`OAuthFlowStore.spend` is `GET` and `DEL` inside `MULTI`/`EXEC`.

**The tests that named that race as their subject could not observe it.**
`tests/unit/test_token_store.py` and `tests/unit/test_oauth_flow.py` drive Redis
fakes whose methods are `async def` with no `await` inside. A coroutine that
never suspends runs to completion the moment it is started, so `asyncio.gather`
executes each one fully before beginning the next and no two callers are ever
inside the operation at once. Replacing `SET NX` with `EXISTS` then `SET`, and
replacing the pipeline with two separate round trips, both left the suite green
(AUTH-01). The controls were killed, so the gap was specifically atomicity.

What closes it is real Redis and a barrier. Every contender is released into
`spend` in the same tick, so each one issues its first command before any of
them reads a reply — which is exactly the interleaving a check-then-write
implementation loses, and is deterministic rather than hopeful:

* against the shipped implementation Redis serialises the operation and exactly
  one caller wins;
* against a check-then-write implementation every contender reads "not spent"
  before any of them writes, and every one of them wins.

So these fail if the atomicity is ever taken out, which is the whole of what
they exist to do. Nothing here asserts anything the unit suites already prove
about the stores' ordinary behaviour.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.oauth_flow import FlowKind, OAuthFlow, OAuthFlowStore
from app.core.token_store import RefreshTokenStore
from tests.fakes import as_redis_client

pytestmark = pytest.mark.integration

# A database of its own, so a run cannot disturb whatever else uses this Redis.
# The queue suites hold 12 and 14, the metric catalogue 13.
REDIS_URL = "redis://localhost:6379/11"

# Enough contenders that a single accidental serialisation could not produce a
# passing result by luck, and few enough that a failure names the interleaving
# rather than the load.
CONTENDERS = 8

TTL_SECONDS = 120
BINDING = "a" * 64


class _RedisClient:
    """What the two stores ask a `RedisClient` for, and nothing else."""

    def __init__(self, client: Redis) -> None:
        self.client = client


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - Redis is not running
        await client.aclose()
        pytest.skip("No Redis reachable; atomicity cannot be observed against a fake.")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _released_together(
    call: object,
    *,
    parties: int,
    barrier: asyncio.Barrier,
) -> list[object]:
    """Run `parties` copies of `call`, all entering the operation at once.

    The barrier is inside the operation under test rather than in front of it -
    see the monkeypatched wrappers below - so what is synchronised is the
    moment each caller starts talking to Redis, not the moment each coroutine
    is created. Synchronising the latter would prove nothing: `gather` already
    starts them together and the old fakes still could not interleave.
    """
    assert barrier.parties == parties
    return list(await asyncio.gather(*(call() for _ in range(parties))))  # type: ignore[operator]


# ------------------------------------------------------------ refresh tokens


async def test_contenders_spending_one_refresh_token_have_exactly_one_winner(
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`False` for everyone but one, however the eight requests interleave.

    This is the property refresh-replay detection is built on. `spend` returning
    `True` twice means two presentations of one token were both issued a fresh
    pair - a stolen token used alongside the real one, and neither noticed.
    """
    token_id = uuid.uuid4()
    barrier = asyncio.Barrier(CONTENDERS)
    original = RefreshTokenStore.spend

    async def synchronised_spend(
        self: RefreshTokenStore,
        token: uuid.UUID,
        *,
        ttl_seconds: int,
    ) -> bool:
        # Released inside the method, so every caller issues its first Redis
        # command before any of them has read a reply.
        await barrier.wait()
        return await original(self, token, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(RefreshTokenStore, "spend", synchronised_spend)

    async def spend() -> bool:
        # A store of its own per caller, over the same Redis, because in
        # production these are separate processes sharing one server.
        store = RefreshTokenStore(as_redis_client(_RedisClient(redis)))
        return await store.spend(token_id, ttl_seconds=TTL_SECONDS)

    outcomes = await _released_together(spend, parties=CONTENDERS, barrier=barrier)

    assert outcomes.count(True) == 1, outcomes
    assert outcomes.count(False) == CONTENDERS - 1, outcomes


async def test_a_refresh_token_spent_under_contention_stays_spent(
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race leaves the denylist in the state a later caller must see.

    A winner that raced and then failed to record itself would be worse than a
    second winner: the token would look live to everything that checks
    afterwards, so the replay control would be off for that token for the rest
    of its fortnight.
    """
    token_id = uuid.uuid4()
    barrier = asyncio.Barrier(CONTENDERS)
    original = RefreshTokenStore.spend

    async def synchronised_spend(
        self: RefreshTokenStore,
        token: uuid.UUID,
        *,
        ttl_seconds: int,
    ) -> bool:
        await barrier.wait()
        return await original(self, token, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(RefreshTokenStore, "spend", synchronised_spend)

    async def spend() -> bool:
        store = RefreshTokenStore(as_redis_client(_RedisClient(redis)))
        return await store.spend(token_id, ttl_seconds=TTL_SECONDS)

    await _released_together(spend, parties=CONTENDERS, barrier=barrier)

    monkeypatch.undo()
    store = RefreshTokenStore(as_redis_client(_RedisClient(redis)))
    assert await store.is_revoked(token_id)
    # And a ninth caller arriving afterwards is a replay like the other seven.
    assert await store.spend(token_id, ttl_seconds=TTL_SECONDS) is False


# --------------------------------------------------------------- OAuth state


async def test_contenders_spending_one_oauth_state_have_exactly_one_winner(
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One callback receives the flow; the rest are told nothing was there.

    The flow record carries the nonce and the PKCE verifier, so a second caller
    receiving it is a second caller able to redeem the authorization code. That
    is the replay `MULTI`/`EXEC` exists to make impossible, and returning the
    payload to everybody while deleting the key once would look identical in
    every sequential test.
    """
    store = OAuthFlowStore(as_redis_client(_RedisClient(redis)))
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)

    barrier = asyncio.Barrier(CONTENDERS)
    original = OAuthFlowStore.spend

    async def synchronised_spend(self: OAuthFlowStore, *, state: str) -> OAuthFlow | None:
        await barrier.wait()
        return await original(self, state=state)

    monkeypatch.setattr(OAuthFlowStore, "spend", synchronised_spend)

    async def spend() -> OAuthFlow | None:
        contender = OAuthFlowStore(as_redis_client(_RedisClient(redis)))
        return await contender.spend(state=started.state)

    outcomes = await _released_together(spend, parties=CONTENDERS, barrier=barrier)

    winners = [flow for flow in outcomes if flow is not None]
    assert len(winners) == 1, outcomes
    assert outcomes.count(None) == CONTENDERS - 1, outcomes

    # The winner got the real record rather than a truncated one, so "exactly
    # one" is not being satisfied by everybody failing except by accident.
    winner = winners[0]
    assert isinstance(winner, OAuthFlow)
    assert winner.kind is FlowKind.LOGIN
    assert winner.nonce == started.flow.nonce
    assert winner.code_verifier == started.flow.code_verifier
    assert winner.binding == BINDING


async def test_an_oauth_state_spent_under_contention_is_gone(
    redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the race and losing the record are the same answer afterwards."""
    store = OAuthFlowStore(as_redis_client(_RedisClient(redis)))
    started = await store.start(kind=FlowKind.LINK, binding=BINDING, user_id=uuid.uuid4())

    barrier = asyncio.Barrier(CONTENDERS)
    original = OAuthFlowStore.spend

    async def synchronised_spend(self: OAuthFlowStore, *, state: str) -> OAuthFlow | None:
        await barrier.wait()
        return await original(self, state=state)

    monkeypatch.setattr(OAuthFlowStore, "spend", synchronised_spend)

    async def spend() -> OAuthFlow | None:
        contender = OAuthFlowStore(as_redis_client(_RedisClient(redis)))
        return await contender.spend(state=started.state)

    await _released_together(spend, parties=CONTENDERS, barrier=barrier)

    monkeypatch.undo()
    assert await store.spend(state=started.state) is None
    # Nothing of the attempt is left behind for anybody to read.
    assert await redis.keys("auth:oauth:flow:*") == []
