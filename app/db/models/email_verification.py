"""Email verification challenges: six-digit codes, stored only as Argon2 hashes.

A challenge is the server's half of "type the code we mailed you". Three
things about it differ from the other secrets in this schema, and each is a
deliberate answer to the fact that a six-digit code is *guessable* in a way a
256-bit token is not.

**The hash is Argon2, not SHA-256.** `hash_reset_token` explains why the reset
and invitation tokens use SHA-256: 256 bits of randomness leave nothing to
brute-force, so a slow hash would only slow the lookup. Six digits is about
twenty bits - a million candidates, microseconds of SHA-256 - so a leaked
database would disclose every live code. Argon2 makes each guess cost real
time. The price is that codes cannot be looked up by hash, since each row is
salted separately; the challenge is found by account instead, which is the
right shape anyway because verification acts on the authenticated caller.

**It records the address it was issued for.** Binding by `user_id` alone
leaves a challenge valid across an email change, which is a genuine bypass:
request a code at an address you control, change the account's address to
somebody else's, submit the code, and the account now claims a verified
address its owner never proved. Comparing `email` to the account's current
address closes that without depending on any future email-change flow
remembering to invalidate anything.

**Attempts are counted on the row.** The code space is small enough that
guessing is a real strategy, so a challenge dies after
``MAX_VERIFICATION_ATTEMPTS`` wrong answers even if the next one would have
been right. Rate limiting bounds the request rate; this bounds the total.

Ending a challenge otherwise follows `PasswordResetToken` exactly.
`consumed_at` is single use, written by an atomic UPDATE so racing
verifications cannot both win. `superseded_at` is written across an account's
live challenges whenever a new code is issued, so asking again narrows the
live surface to one rather than widening it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Wrong answers a single challenge tolerates before it is spent. Five is enough
# for somebody mistyping a code off their phone and nowhere near enough to
# search a million values - the point is that the ceiling exists, not its exact
# height.
MAX_VERIFICATION_ATTEMPTS: Final = 5

# Ten minutes. Long enough to switch to a mail client and back, short enough
# that a code intercepted later is worthless. The service reads this from
# configuration; it is the floor-and-default, not the only value.
DEFAULT_VERIFICATION_TTL_SECONDS: Final = 600

# Argon2 output with its parameters and salt encoded in it, like
# `users.hashed_password`, which this column is sized to match.
MAX_CODE_HASH_LENGTH: Final = 255


class EmailVerificationChallenge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One outstanding "prove you can read this address" request."""

    __tablename__ = "email_verification_challenges"
    __table_args__ = (
        # At most one live challenge per account, enforced by PostgreSQL rather
        # than by the service remembering to supersede first. Two concurrent
        # send requests both supersede and both insert; exactly one row can
        # survive this index, and the loser's IntegrityError is the correct
        # answer rather than a second simultaneously valid code.
        Index(
            "uq_email_verification_challenges_active",
            "user_id",
            unique=True,
            postgresql_where=text("consumed_at IS NULL AND superseded_at IS NULL"),
        ),
        # Every lookup is "the live challenge for this account", since an Argon2
        # hash cannot be searched for.
        Index("ix_email_verification_challenges_user_id", "user_id"),
    )

    # CASCADE, as on password reset tokens: a challenge without its account is
    # a credential kept for nothing.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The address this code was mailed to, captured at issue time. Verification
    # requires it to still equal the account's address, which is what stops a
    # code from surviving an email change. Stored lower-cased, like
    # `users.email`, so the comparison cannot be defeated by capitalisation.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(MAX_CODE_HASH_LENGTH), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_usable(self, *, now: datetime, email: str) -> bool:
        """Whether this challenge can still verify an address.

        Against a caller-supplied clock, like `PasswordResetToken.is_usable`,
        so the rule stays testable without waiting.

        `email` is the account's *current* address and is required rather than
        optional: a caller that forgets to pass it would otherwise silently get
        the weaker check, and the whole point of the column is that the strong
        one cannot be skipped by accident.
        """
        return (
            self.consumed_at is None
            and self.superseded_at is None
            and self.expires_at > now
            and self.attempts < MAX_VERIFICATION_ATTEMPTS
            and self.email == email.strip().lower()
        )
