"""Email verification challenges: issued, read once, and spent atomically.

Every write in this file is a single conditional statement, and that is not
style. A six-digit code is guessable, so the flow above it is the one place in
this application where an attacker has a concrete reason to send many requests
at once - which makes "read it, decide, write it" the wrong shape for anything
that matters here.

The unavoidable complication is that the decision itself cannot be pushed into
SQL. Argon2 hashes are salted per row, so a code cannot be matched by a WHERE
clause the way a reset token's SHA-256 can; the comparison happens in Python,
between a read and a write. `consume` answers that by trusting nothing the read
established and re-checking every precondition as it writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.email_verification import EmailVerificationChallenge


class EmailVerificationRepository:
    """Every query the verification flow makes.

    Unscoped by tenant, like `UserRepository` and
    `PasswordResetTokenRepository`: an address belongs to a global account, not
    to a workspace, so there is no tenant to scope by.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        email: str,
        code_hash: str,
        expires_at: datetime,
    ) -> EmailVerificationChallenge:
        """Record a new challenge.

        Does **not** supersede the account's existing challenges - the caller
        must, in this same transaction, and the partial unique index makes
        forgetting loud rather than silent: a second live row for one account
        cannot exist, so the mistake surfaces as an IntegrityError instead of as
        two simultaneously valid codes.

        The address is lower-cased on the way in, matching `users.email`, so the
        equality check that ties a challenge to the current address cannot be
        defeated by capitalisation.
        """
        challenge = EmailVerificationChallenge(
            user_id=user_id,
            email=email.strip().lower(),
            code_hash=code_hash,
            expires_at=expires_at,
        )
        self._session.add(challenge)
        await self._session.flush()
        return challenge

    async def get_active(self, *, user_id: uuid.UUID) -> EmailVerificationChallenge | None:
        """The account's live challenge, if it has one.

        By account rather than by code, because an Argon2 verifier cannot be
        searched for. Expiry and the attempt cap are deliberately *not* in this
        query: the caller needs to tell "expired" apart from "never existed" to
        pick an audit reason, and `consume` re-checks both anyway, so filtering
        here would only hide information without adding safety.

        At most one row can match - the partial unique index guarantees it - so
        `first()` is not a silent choice between candidates.
        """
        statement = select(EmailVerificationChallenge).where(
            EmailVerificationChallenge.user_id == user_id,
            EmailVerificationChallenge.consumed_at.is_(None),
            EmailVerificationChallenge.superseded_at.is_(None),
        )
        return (await self._session.execute(statement)).scalars().first()

    async def register_failure(self, *, challenge_id: uuid.UUID) -> int | None:
        """Count one wrong answer, and report the new total.

        `attempts = attempts + 1` evaluated by the database. Reading the count
        into Python, adding one and writing it back would let five concurrent
        guesses each read zero and each write one - an attempt cap that caps
        nothing, under precisely the conditions an attacker arranges on purpose.

        Returns `None` if the challenge is no longer live, which is not an
        error: a guess can arrive just after the challenge was consumed or
        superseded, and there is nothing to count against.
        """
        statement = (
            update(EmailVerificationChallenge)
            .where(
                EmailVerificationChallenge.id == challenge_id,
                EmailVerificationChallenge.consumed_at.is_(None),
                EmailVerificationChallenge.superseded_at.is_(None),
            )
            .values(attempts=EmailVerificationChallenge.attempts + 1)
            .returning(EmailVerificationChallenge.attempts)
        )
        row = (await self._session.execute(statement)).first()
        return None if row is None else int(row[0])

    async def consume(
        self,
        *,
        challenge_id: uuid.UUID,
        email: str,
        now: datetime,
        max_attempts: int,
    ) -> bool:
        """Spend a challenge exactly once, however many requests race.

        The security boundary of the whole feature. One
        ``UPDATE ... WHERE ... RETURNING`` - the ADR-039 shape - and every
        condition the caller already checked is checked again here, because the
        Argon2 comparison sits between that read and this write and the world
        can change underneath it:

        - **not consumed, not superseded** - two requests carrying the same
          correct code both pass the comparison; exactly one changes a row.
        - **not expired**, re-evaluated against `now` rather than trusted from
          the read, so a challenge that lapsed mid-request does not verify.
        - **within the attempt cap** - a concurrent wrong guess may have
          exhausted the challenge after the correct code was validated. Without
          this the last guess of an exhausted challenge could still win.
        - **still bound to the same address**, so a code cannot survive an
          email change and verify an address it was never sent to.

        The attempt comparison is ``<=`` here where `is_usable` uses ``<``, and
        the difference is not a slip. The caller counts this attempt *before*
        comparing the code, so by the time this statement runs the row already
        includes the attempt being adjudicated. Against a ceiling of five,
        somebody typing their fifth and final answer arrives here with
        `attempts = 5`; a strict comparison would reject the correct code on
        the last permitted try and report it as a lost race. What must still be
        refused is a total that *exceeds* the ceiling, which is what a
        concurrent guess arriving in the meantime produces.

        Returning `False` is a refusal, not an error. It means another request
        got there first, or one of the conditions stopped holding.
        """
        statement = (
            update(EmailVerificationChallenge)
            .where(
                EmailVerificationChallenge.id == challenge_id,
                EmailVerificationChallenge.consumed_at.is_(None),
                EmailVerificationChallenge.superseded_at.is_(None),
                EmailVerificationChallenge.expires_at > now,
                EmailVerificationChallenge.attempts <= max_attempts,
                EmailVerificationChallenge.email == email.strip().lower(),
            )
            .values(consumed_at=now)
            .returning(EmailVerificationChallenge.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def supersede_outstanding(self, *, user_id: uuid.UUID, now: datetime) -> int:
        """End every live challenge this account holds.

        Called before a new code is issued, so asking twice leaves one usable
        code rather than two. Returns how many were ended, which lets the caller
        record that a resend invalidated something without a second query.
        """
        statement = (
            update(EmailVerificationChallenge)
            .where(
                EmailVerificationChallenge.user_id == user_id,
                EmailVerificationChallenge.consumed_at.is_(None),
                EmailVerificationChallenge.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return int(result.rowcount or 0)
