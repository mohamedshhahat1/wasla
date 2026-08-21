"""Tests for refresh token revocation."""

from __future__ import annotations

import uuid

from app.core.token_store import RefreshTokenStore


class FakeCommands:
    """The two Redis commands the store uses, with expiry recorded."""

    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.expiries[key] = ex

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
