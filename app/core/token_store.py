"""Refresh token revocation.

Access tokens are deliberately not revocable: they live for minutes, and
checking a denylist on every request would surrender the main benefit of
stateless verification for very little. Refresh tokens live for weeks, so each
one is tracked individually by its identifier.
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

    async def revoke(self, token_id: uuid.UUID, *, ttl_seconds: int) -> None:
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
        found = await self._redis.client.exists(self._key(token_id))
        return int(found) > 0
