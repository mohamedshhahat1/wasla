"""User model.

Identity is global: one account per person, reaching tenants through
memberships. There is deliberately no ``tenant_id`` on this table.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import PLATFORM_ROLE_TYPE, PlatformRole


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A person who can sign in.

    Email addresses are stored lower-cased, which makes the unique constraint a
    case-insensitive one; normalisation happens in the repository so every
    write path shares it.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Set when a password is chosen: at registration, or when an invitation is
    # accepted. Hashing arrives with authentication in Phase 2.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
