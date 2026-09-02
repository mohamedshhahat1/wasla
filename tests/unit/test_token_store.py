"""Tests for refresh token revocation."""

from __future__ import annotations

import uuid

import pytest
from redis.exceptions import ConnectionError

from app.core.exceptions import DependencyUnavailableError
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


class FailingCommands(FakeCommands):
    """Redis that is reachable for nothing, the way an outage presents.

    `ConnectionError` rather than a bare `RedisError`, because that is what
    redis-py actually raises when the server is gone and it is the subclass a
    handler most plausibly gets wrong.
    """

    def __init__(self, *, message: str = "Error connecting to redis:6379"):
        super().__init__()
        self._message = message

    async def set(self, key, value, ex=None, nx=False):
        raise ConnectionError(self._message)

    async def exists(self, key):
        raise ConnectionError(self._message)


class FailingRedis:
    def __init__(self, *, message: str = "Error connecting to redis:6379"):
        self.client = FailingCommands(message=message)


async def test_spending_a_token_refuses_rather_than_guessing_when_redis_is_down():
    store = RefreshTokenStore(FailingRedis())

    # Not `True`. An unreachable denylist answering "you are the first" is a
    # replay control failing open, and the caller would be handed a fresh
    # session on the strength of a token nobody could check.
    with pytest.raises(DependencyUnavailableError) as refusal:
        await store.spend(uuid.uuid4(), ttl_seconds=60)

    assert refusal.value.status_code == 503
    assert refusal.value.error_code == "dependency_unavailable"
    assert refusal.value.details == {"dependency": "redis"}


async def test_revoking_refuses_rather_than_claiming_a_logout_that_did_not_happen():
    store = RefreshTokenStore(FailingRedis())

    # Returning quietly here would answer 204 to somebody signing out of a
    # shared machine while their refresh token stayed usable for weeks.
    with pytest.raises(DependencyUnavailableError):
        await store.revoke(uuid.uuid4(), ttl_seconds=60)


async def test_reading_the_denylist_refuses_rather_than_reporting_not_revoked():
    store = RefreshTokenStore(FailingRedis())

    # "Not found" and "could not look" are different answers, and only one of
    # them means the token is still live.
    with pytest.raises(DependencyUnavailableError):
        await store.is_revoked(uuid.uuid4())


async def test_an_expired_token_is_answered_without_reaching_redis_at_all():
    store = RefreshTokenStore(FailingRedis())
    token_id = uuid.uuid4()

    # Both short-circuit on the TTL before any command is issued, so a token
    # that is already dead is still answered during an outage rather than
    # turning a no-op logout into a 503.
    assert await store.spend(token_id, ttl_seconds=0) is False
    await store.revoke(token_id, ttl_seconds=0)


async def test_the_refusal_carries_no_connection_detail():
    """The message a person sees names the service, never the server.

    redis-py puts the host and port it failed to reach into `ConnectionError`,
    and a URL configured with a password is one string away from that. The
    dependency name is the whole of what a caller is told.
    """
    store = RefreshTokenStore(FailingRedis(message="Error connecting to redis://:hunter2@db:6379"))

    with pytest.raises(DependencyUnavailableError) as refusal:
        await store.spend(uuid.uuid4(), ttl_seconds=60)

    rendered = f"{refusal.value.message} {refusal.value.details}"
    assert "hunter2" not in rendered
    assert "redis://" not in rendered
    assert "6379" not in rendered
    assert "ConnectionError" not in rendered


async def test_the_store_works_again_once_redis_returns():
    """No sticky failure state: the outage is per call, not per store."""
    redis = FakeRedis()
    store = RefreshTokenStore(redis)
    token_id = uuid.uuid4()

    broken = FailingCommands()
    working = redis.client
    redis.client = broken
    with pytest.raises(DependencyUnavailableError):
        await store.spend(token_id, ttl_seconds=60)

    redis.client = working
    # And the token that could not be spent during the outage was never
    # recorded, so its holder is not locked out afterwards.
    assert await store.spend(token_id, ttl_seconds=60) is True
