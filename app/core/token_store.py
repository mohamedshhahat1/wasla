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

**A Redis outage refuses the operation (ADR-064).** Every method here is part of
a replay control, so there is no reading of "Redis did not answer" that lets one
proceed: `spend` returning `True` on an unreachable store would hand a fresh
session to a replayed token, and `revoke` returning quietly would tell somebody
they had signed out when nothing was written down. Both raise
`DependencyUnavailableError`, which the handler renders as a 503 naming `redis`
and nothing else - the same shape `OAuthFlowStore` already uses, and the same
argument ADR-051 makes for it. `RateLimiter` degrades instead (ADR-040) because
a limiter meters capacity; this meters credentials.

The blast radius is stated rather than discovered: while Redis is unreachable
nobody can refresh a session, so every session in the estate ends within the
access-token lifetime. That is the cost of not failing open, and it is the right
side to be on - `users.token_version` still revokes in bulk through PostgreSQL,
so signing out everywhere keeps working throughout.
"""

from __future__ import annotations

import uuid
from typing import Final

from redis.exceptions import RedisError

from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

KEY_PREFIX: Final = "auth:refresh:revoked:"
MARKER: Final = "1"

# Said to a caller whose session could not be checked. Deliberately about the
# service rather than about them: nothing they hold is wrong, and telling them
# their credentials failed would send them to reset a password that is fine.
UNAVAILABLE: Final = "Sessions are temporarily unavailable. Please try again shortly."


def _unavailable(exc: RedisError, *, operation: str) -> DependencyUnavailableError:
    """The one refusal every method here raises, logged once and shaped once.

    The exception *type* is recorded and its message is not. A `ConnectionError`
    from redis-py carries the connection it failed to make, which is a URL that
    can hold a password - and this is the log line most likely to be read by
    somebody pasting it into a ticket.
    """
    logger.warning(
        "auth.token_store_unavailable",
        extra={
            "event": "auth.token_store_unavailable",
            "operation": operation,
            "reason": type(exc).__name__,
        },
    )
    return DependencyUnavailableError(UNAVAILABLE, details={"dependency": "redis"})


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

        :raises DependencyUnavailableError: Redis could not be reached, so
            single use cannot be guaranteed. Deliberately not `True`: an
            unreachable denylist that answers "you are the first" is a replay
            control failing open, and the caller would be handed a new session
            on the strength of a token nobody could check.
        """
        if ttl_seconds <= 0:
            return False
        try:
            won = await self._redis.client.set(
                self._key(token_id),
                MARKER,
                ex=ttl_seconds,
                nx=True,
            )
        except RedisError as exc:
            raise _unavailable(exc, operation="spend") from exc
        # redis-py returns True on a successful NX write and None when the key
        # was already there. Normalised so callers can branch on a bool.
        return bool(won)

    async def revoke(self, token_id: uuid.UUID, *, ttl_seconds: int) -> None:
        """Mark a token spent, without caring whether it already was.

        For logging out, where "it was already revoked" is not an error and
        there is nothing to detect.

        :raises DependencyUnavailableError: the revocation could not be
            written. Returning quietly would answer 204 to somebody signing out
            of a shared machine while their refresh token stayed usable for
            weeks - a false statement about a security action, which is worse
            than an honest failure they can retry.
        """
        if ttl_seconds <= 0:
            # Already expired: there is nothing left to revoke.
            return
        try:
            await self._redis.client.set(self._key(token_id), MARKER, ex=ttl_seconds)
        except RedisError as exc:
            raise _unavailable(exc, operation="revoke") from exc
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

        :raises DependencyUnavailableError: for the reason `spend` gives.
            "Not found" and "could not look" are different answers, and only
            one of them means the token is live.
        """
        try:
            found = await self._redis.client.exists(self._key(token_id))
        except RedisError as exc:
            raise _unavailable(exc, operation="is_revoked") from exc
        return int(found) > 0
