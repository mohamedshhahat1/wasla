"""Short-lived proof that somebody re-proved who they are.

A Google-only account has no password, and `DELETE /auth/me` needs proof beyond
a session - a stolen access token is a session. Until now such an account was
told to set a password first, which is secure and is a poor answer: it makes
somebody acquire a credential they never wanted in order to leave.

This is the other answer. The person is sent back through Google, the callback
checks that the identity that came back is the one already linked to their
account, and what it leaves behind is a **proof**: a short-lived, single-use
server-side record that `DELETE /auth/me` consumes.

## Why a proof rather than deleting in the callback

The callback could delete the account there and then, and it would be fewer
moving parts. It is the wrong shape for three reasons.

**A GET-shaped browser redirect would become a destructive action.** The
callback is reached by Google sending a browser to it. Deletion belongs on a
`DELETE` the client makes deliberately, not on a navigation.

**The client needs somewhere to put the confirmation dialogue.** "Are you sure"
comes after the identity check, not before Google.

**It keeps one deletion path.** Password accounts and Google accounts converge
on the same endpoint with the same effects; only the proof differs. A second
deletion path inside the OAuth callback is a second place for the ownership
rules, the session revocation and the audit entry to drift.

## What the proof is, and what it is not

It is a random opaque token, stored server-side in Redis under its **SHA-256
digest**, carrying the user it was issued to and the purpose it was issued for.

* **Single use.** Spent with the same `GET`-then-`DELETE` pipeline the OAuth
  state store uses, so two requests racing one proof have exactly one winner.
* **Short-lived.** Minutes, not hours - long enough to read a confirmation
  dialogue and press a button.
* **Bound to a user.** A proof issued to one account cannot authorise deleting
  another, checked on consumption rather than trusted from the request.
* **Bound to a purpose.** It authorises account deletion and nothing else.
  Adding a second high-risk action means a second purpose, not a wider proof.
* **Not a credential.** It grants no session, opens no route, and is useless
  without the access token of the account it belongs to.

**Only the digest is stored.** Somebody who reads the Redis keyspace learns that
a proof exists and cannot present one, which is the same property the OAuth
state store gets from storing only the binding digest.

## Fail closed

Every Redis failure raises rather than returning "no proof" or, worse, "proof
accepted". A replay control that fails open is not a control, and this one
stands in front of an irreversible action.
"""

from __future__ import annotations

import hashlib
import json
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

KEY_PREFIX: Final = "reauth:"
# 256 bits. The proof is presented by a client that already holds an access
# token for the account, so this is not the only thing standing in the way -
# but it is the thing that says "you proved yourself a minute ago", and that
# should not be guessable.
TOKEN_BYTES: Final = 32
# Five minutes. Long enough to read a confirmation dialogue and decide; short
# enough that a proof left in a browser's history on a shared machine is inert
# by the time anybody finds it.
PROOF_TTL_SECONDS: Final = 300


class ReauthPurpose(StrEnum):
    """What a proof authorises.

    Checked on consumption, so a proof minted for one purpose cannot be spent
    on another. One member today; the enum exists so that the second high-risk
    action is a new value rather than a widening of this one.
    """

    DELETE_ACCOUNT = "delete_account"


@dataclass(frozen=True, slots=True)
class ReauthProof:
    """A completed re-authentication, as remembered server-side."""

    user_id: uuid.UUID
    purpose: ReauthPurpose


def _digest(token: str) -> str:
    """What is stored. The token itself never reaches Redis."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _key(token: str) -> str:
    return f"{KEY_PREFIX}{_digest(token)}"


def _decode(payload: str) -> ReauthProof | None:
    """Rebuild a proof, or decide there is none.

    Anything unreadable is absent rather than raised, matching the OAuth flow
    store: a corrupt record is not a caller's fault to be told about, and the
    safe reading of "this does not parse" is "this is not a proof I issued".
    """
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    user_id = raw.get("user_id")
    purpose = raw.get("purpose")
    if not isinstance(user_id, str) or purpose not in tuple(item.value for item in ReauthPurpose):
        return None
    try:
        return ReauthProof(user_id=uuid.UUID(user_id), purpose=ReauthPurpose(purpose))
    except ValueError:
        return None


class ReauthProofStore:
    """Issues and spends re-authentication proofs."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def issue(self, *, user_id: uuid.UUID, purpose: ReauthPurpose) -> str:
        """Mint a proof and return the token, which is shown once and not stored.

        The caller hands the token to the person who just proved themselves.
        Nothing can recover it afterwards - only its digest is kept - so a
        proof that is lost is re-earned rather than looked up.
        """
        token = secrets.token_urlsafe(TOKEN_BYTES)
        payload = json.dumps({"user_id": str(user_id), "purpose": purpose.value})
        try:
            # `nx` so this can only ever create, for the reason the flow store
            # gives: a collision must not silently overwrite somebody else's
            # in-flight proof.
            created = await self._redis.client.set(
                _key(token),
                payload,
                ex=PROOF_TTL_SECONDS,
                nx=True,
            )
        except RedisError as exc:
            logger.warning(
                "reauth.store_unavailable",
                extra={"event": "reauth.store_unavailable", "phase": "issue"},
            )
            raise DependencyUnavailableError(
                "Re-authentication is temporarily unavailable.",
                details={"dependency": "redis"},
            ) from exc

        if not created:  # pragma: no cover - 256 bits collided
            logger.error("reauth.token_collision", extra={"event": "reauth.token_collision"})
            raise DependencyUnavailableError(
                "Re-authentication is temporarily unavailable.",
                details={"dependency": "redis"},
            )
        return token

    async def spend(self, *, token: str) -> ReauthProof | None:
        """Consume a proof exactly once.

        Returns ``None`` for every failure a caller may learn about - missing,
        unknown, expired, already spent, malformed - because they are
        indistinguishable to somebody guessing.

        The caller must still check the proof against the user making the
        request and against the purpose it needs. This returns what the proof
        *says*; it does not decide whether that is what the caller wanted.

        :raises DependencyUnavailableError: Redis could not be reached, so
            single use cannot be guaranteed. Deliberately not ``None``, which
            would be a replay control failing open in front of an irreversible
            action.
        """
        if not token:
            return None
        key = _key(token)
        try:
            async with self._redis.client.pipeline(transaction=True) as pipe:
                pipe.get(key)
                pipe.delete(key)
                payload, deleted = await pipe.execute()
        except RedisError as exc:
            logger.warning(
                "reauth.store_unavailable",
                extra={"event": "reauth.store_unavailable", "phase": "spend"},
            )
            raise DependencyUnavailableError(
                "Re-authentication is temporarily unavailable.",
                details={"dependency": "redis"},
            ) from exc

        # `deleted` is the single-use answer: two requests racing one proof both
        # read the same payload, and only one of them removed it.
        if not deleted or not isinstance(payload, str):
            return None
        return _decode(payload)
