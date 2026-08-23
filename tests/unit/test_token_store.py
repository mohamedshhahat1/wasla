"""Tests for refresh token revocation."""

from __future__ import annotations

import uuid

from app.core.token_store import RefreshTokenStore


class FakeCommands:
    """The Redis commands the store uses, with expiry recorded.

    `set` implements `nx` faithfully - no write and a None return when the key
    exists - because that is the entire mechanism behind replay detection.
    """

    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def exists(self, key):
        return 1 if key in self.values else 0


class FakeRedis:
    def __init__(self):
        self.client = FakeCommands()


async def test_a_revoked_token_is_reported_as_revoked():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    assert not await store.is_revoked(token_id)

    await store.revoke(token_id, ttl_seconds=60)

    assert await store.is_revoked(token_id)


async def test_the_record_expires_with_the_token():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)

    await store.revoke(uuid.uuid4(), ttl_seconds=60)

    # A token that can no longer be verified needs no record, so the denylist
    # cannot grow without bound.
    assert set(redis.client.expiries.values()) == {60}


async def test_an_already_expired_token_is_not_recorded():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    await store.revoke(token_id, ttl_seconds=0)

    assert redis.client.values == {}
    assert not await store.is_revoked(token_id)


async def test_tokens_are_tracked_one_by_one():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    spent = uuid.uuid4()
    still_good = uuid.uuid4()

    await store.revoke(spent, ttl_seconds=60)

    assert await store.is_revoked(spent)
    assert not await store.is_revoked(still_good)


async def test_spending_a_token_succeeds_once_and_then_reports_a_replay():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    assert await store.spend(token_id, ttl_seconds=60) is True
    # The second presentation of the same token is the signal the refresh path
    # tears an account's session estate down on.
    assert await store.spend(token_id, ttl_seconds=60) is False


async def test_spending_records_the_token_for_exactly_its_remaining_life():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)

    await store.spend(uuid.uuid4(), ttl_seconds=45)

    assert set(redis.client.expiries.values()) == {45}


async def test_an_expired_token_cannot_be_spent():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)

    # Nothing left to record, and nobody presenting one is entitled to a new
    # pair, so refusing is right on both readings.
    assert await store.spend(uuid.uuid4(), ttl_seconds=0) is False
    assert redis.client.values == {}


async def test_a_token_revoked_by_logout_cannot_then_be_spent():
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    await store.revoke(token_id, ttl_seconds=60)

    # Logging out and then refreshing with the same token is a replay by the
    # same definition, and is treated as one.
    assert await store.spend(token_id, ttl_seconds=60) is False


async def test_concurrent_spends_of_one_token_produce_exactly_one_winner():
    import asyncio

    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    results = await asyncio.gather(
        *(store.spend(token_id, ttl_seconds=60) for _ in range(8)),
    )

    assert results.count(True) == 1
    assert results.count(False) == 7
