"""In-flight OAuth authorization attempts.

One record per attempt, in Redis, under a key nobody can guess. It holds the
values that have to survive a round trip through somebody else's website: the
nonce that will appear in the ID token, the PKCE verifier that will redeem the
authorization code, the digest of the secret held by the browser that started
it, and - for a linking attempt - which account started it.

Keeping them together is deliberate. Three separate stores would be three
chances to validate one and forget another; here there is no way to check the
state and skip the nonce, because they arrive as one object or not at all.

**Spending is one operation.** `GET` and `DEL` inside `MULTI`/`EXEC`, so of two
callbacks presenting the same state exactly one sees the delete succeed. Read,
validate, then delete would be a race whose losing branch is a replayed
authorization - the same reasoning `RefreshTokenStore.spend` gives under
ADR-039: whether the key was still there *is* the answer. `GETDEL` would do it
in one round trip but requires Redis 6.2, and this code cannot assume what
version a deployment runs.

**A Redis outage refuses the callback (ADR-051).** ADR-040 lets a rate limiter
fall back to a process-local window, because refusing traffic during an
infrastructure outage is worse than allowing it. That argument does not carry
over. A process-local state store breaks as soon as uvicorn runs more than one
worker: the callback lands on a process that never issued the state, so "not
found" would have to mean "accept anyway" for the flow to work at all. That is
not degraded capacity, it is a disabled replay control, and ADR-040 itself says
security controls must not fail open. Google sign-in becomes unavailable;
password login is untouched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from redis.exceptions import RedisError

from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

KEY_PREFIX: Final = "auth:oauth:flow:"

# 32 bytes each, url-safe base64 encoded - 43 characters, 256 bits of entropy.
# `secrets`, never `random`: the module that seeds from the system CSPRNG rather
# than the one that is reproducible on purpose.
STATE_BYTES: Final = 32
NONCE_BYTES: Final = 32
# RFC 7636 requires the verifier to be 43-128 characters. 32 bytes encoded is
# exactly 43, which is the minimum the specification permits and 256 bits of
# entropy - the length limit is about URL safety, not about strength.
VERIFIER_BYTES: Final = 32

# Long enough for somebody to find their password manager and pick an account,
# short enough that a state left in a browser history is useless by the time
# anybody reads it.
FLOW_TTL_SECONDS: Final = 600

# What a state may look like, checked before the value is used to build a Redis
# key. `token_urlsafe(32)` produces 43 characters from this alphabet, so this
# accepts everything this module issues and refuses a megabyte of
# caller-supplied junk becoming a lookup. A state that fails this never existed,
# which is the same answer as a state that expired.
#
# `MAX_STATE_LENGTH` is exported because the request schema bounds the field with
# it too. One constant, so the edge and the store cannot disagree about how long
# a state may be.
MIN_STATE_LENGTH: Final = 16
MAX_STATE_LENGTH: Final = 128
_STATE_SHAPE: Final = re.compile(rf"^[A-Za-z0-9_-]{{{MIN_STATE_LENGTH},{MAX_STATE_LENGTH}}}$")


class FlowKind(StrEnum):
    """What the attempt was started for.

    Recorded so that a flow begun as a login cannot be completed as a link, or
    the reverse. Without it, the two endpoints would share a state namespace and
    an attacker could start whichever flow has the weaker checks and finish it
    at the other.
    """

    LOGIN = "login"
    LINK = "link"


@dataclass(frozen=True, slots=True)
class OAuthFlow:
    """What was remembered when the browser was sent to Google."""

    kind: FlowKind
    nonce: str
    code_verifier: str
    # SHA-256 of the secret handed to the initiating browser in a cookie. This
    # is what binds the attempt to a browser rather than only to this server
    # (ADR-066): the state proves we issued it, and this proves who asked. Only
    # the digest is here, so a reader of this keyspace cannot complete a flow.
    binding: str
    # Set only for a link. This is what binds a linking attempt to one account:
    # the identity is attached to whoever started the flow, never to whoever
    # matches the email address in the token.
    user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class StartedFlow:
    """A new attempt: the opaque handle, and what it stands for."""

    state: str
    flow: OAuthFlow


def code_challenge(verifier: str) -> str:
    """The S256 PKCE challenge for a verifier (RFC 7636).

    SHA-256, url-safe base64, no padding. The `plain` method is not implemented
    and should not be: it puts the verifier itself in the authorization URL,
    which defeats the point of having one.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _key(state: str) -> str:
    return f"{KEY_PREFIX}{state}"


def _encode(flow: OAuthFlow) -> str:
    return json.dumps(
        {
            "kind": flow.kind.value,
            "nonce": flow.nonce,
            "code_verifier": flow.code_verifier,
            "binding": flow.binding,
            "user_id": str(flow.user_id) if flow.user_id else None,
        }
    )


def _decode(payload: str) -> OAuthFlow | None:
    """Rebuild a flow, or decide there is none.

    Anything unreadable is treated as absent rather than raised. A corrupt
    record is not a caller's fault to be told about, and the safe reading of
    "this does not parse" is "this is not a flow I issued".
    """
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    kind = raw.get("kind")
    nonce = raw.get("nonce")
    verifier = raw.get("code_verifier")
    binding = raw.get("binding")
    if kind not in tuple(item.value for item in FlowKind):
        return None
    if not isinstance(nonce, str) or not nonce:
        return None
    if not isinstance(verifier, str) or not verifier:
        return None
    if not isinstance(binding, str) or not binding:
        # Required, not defaulted. A record without one is either corrupt or
        # was written by a build that predates browser binding, and treating
        # either as "no binding needed" would be a check that switches itself
        # off. The cost is that flows in flight across a deploy are refused;
        # they live ten minutes and the person retries.
        return None

    user_id: uuid.UUID | None = None
    raw_user = raw.get("user_id")
    if raw_user is not None:
        if not isinstance(raw_user, str):
            return None
        try:
            user_id = uuid.UUID(raw_user)
        except ValueError:
            return None

    return OAuthFlow(
        kind=FlowKind(kind),
        nonce=nonce,
        code_verifier=verifier,
        binding=binding,
        user_id=user_id,
    )


class OAuthFlowStore:
    """Issues and spends authorization attempts."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def start(
        self,
        *,
        kind: FlowKind,
        binding: str,
        user_id: uuid.UUID | None = None,
    ) -> StartedFlow:
        """Mint an attempt and remember it for :data:`FLOW_TTL_SECONDS`.

        `binding` is the digest of the initiating browser's secret, computed by
        the caller. This store never sees the secret itself, which is what keeps
        the value in Redis from being enough to finish the flow.
        """
        state = secrets.token_urlsafe(STATE_BYTES)
        flow = OAuthFlow(
            kind=kind,
            nonce=secrets.token_urlsafe(NONCE_BYTES),
            code_verifier=secrets.token_urlsafe(VERIFIER_BYTES),
            binding=binding,
            user_id=user_id,
        )

        try:
            # `nx` so this can only ever create. Without it a collision would
            # silently overwrite somebody else's in-flight attempt, and the
            # first person to finish would fail for no discoverable reason.
            created = await self._redis.client.set(
                _key(state),
                _encode(flow),
                ex=FLOW_TTL_SECONDS,
                nx=True,
            )
        except RedisError as exc:
            logger.warning(
                "oauth.flow_store_unavailable",
                extra={
                    "event": "oauth.flow_store_unavailable",
                    "phase": "start",
                    "reason": type(exc).__name__,
                },
            )
            raise DependencyUnavailableError(
                "Google sign-in is temporarily unavailable.",
                details={"dependency": "redis"},
            ) from exc

        if not created:
            # 256 bits collided, or something else is writing this keyspace.
            # Both are worth knowing about and neither is the caller's fault.
            logger.error("oauth.state_collision", extra={"event": "oauth.state_collision"})
            raise DependencyUnavailableError(
                "Google sign-in is temporarily unavailable.",
                details={"dependency": "redis"},
            )

        return StartedFlow(state=state, flow=flow)

    async def spend(self, *, state: str) -> OAuthFlow | None:
        """Consume an attempt exactly once.

        Returns ``None`` for every kind of failure a caller is allowed to learn
        about - missing, unknown, expired, already spent, malformed - because
        they are indistinguishable to somebody guessing, and telling them apart
        would only help them guess better.

        :raises DependencyUnavailableError: Redis could not be reached, so
            single use cannot be guaranteed. Deliberately not ``None``: that
            would be a replay control failing open.
        """
        if not _STATE_SHAPE.match(state):
            return None

        key = _key(state)
        try:
            async with self._redis.client.pipeline(transaction=True) as pipe:
                pipe.get(key)
                pipe.delete(key)
                payload, deleted = await pipe.execute()
        except RedisError as exc:
            logger.warning(
                "oauth.flow_store_unavailable",
                extra={
                    "event": "oauth.flow_store_unavailable",
                    "phase": "spend",
                    "reason": type(exc).__name__,
                },
            )
            raise DependencyUnavailableError(
                "Google sign-in is temporarily unavailable.",
                details={"dependency": "redis"},
            ) from exc

        # `deleted` is the single-use answer. Two callbacks racing on one state
        # both read the same payload, and only one of them removed it.
        if not deleted or not isinstance(payload, str):
            return None
        return _decode(payload)
