"""User model.

Identity is global: one account per person, reaching tenants through
memberships. There is deliberately no ``tenant_id`` on this table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import PLATFORM_ROLE_TYPE, PlatformRole

# The widths of the three columns an external identity provider can write into,
# named because something outside this module has to check against them.
#
# A validator that repeated the number would be a second copy of a decision, and
# the failure mode of the two disagreeing is silent: the value passes the check,
# reaches PostgreSQL, and raises `DataError` - which is not `IntegrityError`, so
# it escapes the handlers that exist and becomes a 500 (SEC-11). Exporting the
# constants is what makes "bounded before persistence" checkable rather than
# remembered.
MAX_EMAIL_LENGTH: Final = 320
MAX_FULL_NAME_LENGTH: Final = 200
MAX_AVATAR_URL_LENGTH: Final = 512


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A person who can sign in.

    Email addresses are stored lower-cased, which makes the unique constraint a
    case-insensitive one; normalisation happens in the repository so every
    write path shares it.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    email: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH), nullable=False)
    # Set when a password is chosen: at registration, or when an invitation is
    # accepted. Hashing arrives with authentication in Phase 2.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(MAX_FULL_NAME_LENGTH), nullable=True)
    # An avatar the product can render, wherever it came from. Deliberately a
    # column on the account rather than on `user_identities`: what the interface
    # needs is "this person's picture", and a field that had to be resolved by
    # asking which issuer vouched most recently would be answering a different
    # question. Today only Google sign-in populates it.
    #
    # Sized to `MAX_PICTURE_URL_LENGTH` in the Google verifier, which refuses
    # anything longer before it reaches here. Nothing renders this without the
    # verifier having already established it is an https URL - the column holds
    # no other kind of value, and nothing else writes it.
    avatar_url: Mapped[str | None] = mapped_column(String(MAX_AVATAR_URL_LENGTH), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When somebody last proved they could read mail at `email` above.
    #
    # A timestamp rather than a boolean, because "when" is the question actually
    # asked later: a flag cannot tell you whether the proof predates a support
    # ticket, and it cannot be compared against the moment an address changed.
    #
    # NULL means unverified, and unverified is a completely ordinary state. This
    # column grants nothing (docs/EMAIL_VERIFICATION.md): no route reads it, no
    # permission depends on it, and authentication does not consult it. It is an
    # account-integrity fact, not an authorization input - and it must not
    # quietly become one, because a column that starts gating things is a column
    # that locks out every account created before it existed.
    #
    # Any future flow that changes `email` must set this back to NULL. It does
    # not also have to hunt down outstanding challenges: each one records the
    # address it was issued for and is checked against the current one, so a
    # code sent to a previous address cannot verify a new one.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Bumped to invalidate every token this person holds (ADR-036).
    #
    # Every access and refresh token carries the value that was current when it
    # was minted, and both are checked against this column on use, so raising it
    # by one revokes an entire session estate in a single UPDATE. It is the
    # answer to "a refresh token leaked, kill it now": rotation only spends the
    # token that is presented, which is the victim's rather than the thief's.
    #
    # Deliberately per *user* rather than per session. Signing one device out
    # while leaving another alone needs a row per token, and this product has no
    # device-management surface to justify one; if that changes, this column
    # stays as the coarse lever beside it rather than being replaced.
    token_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    platform_role: Mapped[PlatformRole | None] = mapped_column(
        PLATFORM_ROLE_TYPE,
        nullable=True,
        default=None,
    )

    @property
    def is_platform_staff(self) -> bool:
        """Platform staff act across tenants. Absence of a role is the norm."""
        return self.platform_role is not None

    @property
    def is_email_verified(self) -> bool:
        """Whether inbox ownership has ever been proven for the current address.

        A convenience for reading, not a permission. Nothing in the application
        may branch on this to decide access - see docs/EMAIL_VERIFICATION.md.
        """
        return self.email_verified_at is not None
