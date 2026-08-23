"""Refresh token revocation.

Access tokens are deliberately not revocable here: they live for minutes, and
checking a denylist on every request would surrender the main benefit of
stateless verification for very little. (They *are* revocable in bulk, through
`users.token_version` - see ADR-036.) Refresh tokens live for weeks, so each one
is tracked individually by its identifier.

**Spending a token is one atomic operation, not a check followed by a write**
(ADR-039). `is_revoked` then `revoke` is a race: two requests carrying the same
token both read "not spent", both write, and both are issued a fresh pair. That
is precisely the shape of a stolen token being used alongside the real one, so
the read-then-write version cannot detect the thing it exists to detect. `spend`
does it in a single `SET NX`, and whether the key already existed *is* the
answer.
"""

from __future__ import annotations

import uuid
from typing import Final

from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

KEY_PREFIX: Final = "auth:refresh:revoked:"
MARKER: Final = "1"


class RefreshTokenStore:
    """Redis-backed denylist of spent and revoked refresh tokens.

    Entries expire together with the token they revoke, so the list is
    self-cleaning: a token that can no longer be verified needs no record.
    """

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    def _key(self, token_id: uuid.UUID) -> str:
        return f"{KEY_PREFIX}{token_id}"

    async def spend(self, token_id: uuid.UUID, *, ttl_seconds: int) -> bool:
        """Mark a token spent, returning whether this caller was the first.

        `False` means the token had already been spent - a replay. The
        distinction is made by Redis under a single key, so of two concurrent
        presentations of the same token exactly one can win, no matter how the
        two requests interleave.

        An already-expired token returns `False` as well. There is nothing left
        to record, and a caller presenting one is not entitled to a new pair
        either, so refusing is right in both readings.
        """
        if ttl_seconds <= 0:
            return False
        won = await self._redis.client.set(
            self._key(token_id),
            MARKER,
            ex=ttl_seconds,
            nx=True,
        )
        # redis-py returns True on a successful NX write and None when the key
        # was already there. Normalised so callers can branch on a bool.
        return bool(won)

    async def revoke(self, token_id: uuid.UUID, *, ttl_seconds: int) -> None:
        """Mark a token spent, without caring whether it already was.

        For logging out, where "it was already revoked" is not an error and
        there is nothing to detect.
        """
        if ttl_seconds <= 0:
            # Already expired: there is nothing left to revoke.
            return
        await self._redis.client.set(self._key(token_id), MARKER, ex=ttl_seconds)
        logger.info(
            "auth.refresh_token_revoked",
            extra={
                "event": "auth.refresh_token_revoked",
                "token_id": str(token_id),
            },
        )

    async def is_revoked(self, token_id: uuid.UUID) -> bool:
        """Whether this token has been spent.

        A read, so it cannot be used to *take* a token: the refresh path uses
        `spend` for that. This exists for diagnostics and for callers that only
        want to know.
        """
        found = await self._redis.client.exists(self._key(token_id))
        return int(found) > 0
