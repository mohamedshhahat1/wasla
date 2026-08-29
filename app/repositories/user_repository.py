"""User data access."""

from __future__ import annotations

import uuid

from sqlalchemy import update

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

    async def bump_token_version(self, user_id: uuid.UUID) -> int | None:
        """Raise `token_version` in the database, atomically, and return it.

        `user.token_version += 1` reads a value into Python and writes it back,
        which loses an increment when two requests do it at once. That is
        tolerable where a person presses a button twice; it is not tolerable on
        the refresh-reuse path (ADR-039), where simultaneous replays are the
        expected shape of an attack and the whole response is a single
        increment. Doing it as one `UPDATE ... RETURNING` makes concurrent
        teardowns compose instead of cancelling out.

        `synchronize_session=False` because the row is refreshed below rather
        than patched in the identity map, and returns None when no such user
        exists, so a caller can tell "nothing to revoke" from "revoked".
        """
        statement = (
            update(User)
            .where(User.id == user_id)
            .values(token_version=User.token_version + 1)
            .returning(User.token_version)
        )
        result = await self._session.execute(
            statement,
            execution_options={"synchronize_session": False},
        )
        version = result.scalar_one_or_none()
        return int(version) if version is not None else None

    async def create(
        self,
        *,
        email: str,
        full_name: str | None = None,
        hashed_password: str | None = None,
        avatar_url: str | None = None,
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
                avatar_url=avatar_url,
                is_active=True,
            )
        )
