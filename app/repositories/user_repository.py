"""User data access."""

from __future__ import annotations

import uuid

from app.core.exceptions import ConflictError
from app.db.models import User
from app.repositories.base import BaseRepository


def normalise_email(email: str) -> str:
    """Email addresses are compared case-insensitively, so they are stored lower-cased."""
    return email.strip().lower()


class UserRepository(BaseRepository[User]):
    """Global user identities.

    Not tenant-scoped: an identity exists before, and independently of, any
    workspace. What a user may do inside a workspace is decided by their
    membership, never by this table.
    """

    model = User

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._first(self._select().where(User.id == user_id))

    async def get_by_email(self, email: str) -> User | None:
        return await self._first(self._select().where(User.email == normalise_email(email)))

    async def create(
        self,
        *,
        email: str,
        full_name: str | None = None,
        hashed_password: str | None = None,
    ) -> User:
        """Create an identity.

        The password hash is optional: an invited user has an account before
        they choose a password. Hashing itself belongs to Phase 2, and a plain
        password must never reach this method.
        """
        normalised = normalise_email(email)
        if await self.get_by_email(normalised) is not None:
            raise ConflictError("An account with that email address already exists.")
        return self.add(
            User(
                email=normalised,
                full_name=full_name.strip() if full_name else None,
                hashed_password=hashed_password,
                is_active=True,
            )
        )
