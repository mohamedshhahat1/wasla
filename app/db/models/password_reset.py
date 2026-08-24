"""Password reset tokens: proof of inbox ownership, stored only as hashes.

A reset serves somebody who cannot sign in, so the token is the credential -
which is why only its SHA-256 lives here. A stolen database yields nothing
usable: the value that resets a password exists solely in the message sent
to the address on file (the invitation table's reasoning, applied to the
flow docs/SECURITY.md deferred until email existed).

Two timestamps end a token's life besides expiry. `consumed_at` is single
use, written by an atomic UPDATE so racing confirmations cannot both win.
`superseded_at` is written across an account's outstanding tokens whenever a
new one is issued or a reset succeeds, so repeated requests narrow the live
surface to one token rather than extending it indefinitely.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One reset link's server-side half."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
        Index("ix_password_reset_tokens_user_id", "user_id"),
    )

    # CASCADE: a token is meaningless without its account, and keeping one
    # after account deletion would be keeping a credential for nothing.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_usable(self, *, now: datetime) -> bool:
        """Whether this token can still reset a password.

        Evaluated against a caller-supplied clock, like the invitation's
        `is_open`, so the rule stays testable.
        """
        return self.consumed_at is None and self.superseded_at is None and self.expires_at > now
