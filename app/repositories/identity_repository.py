"""Data access for federated identities.

Not tenant-scoped, for the same reason `UserRepository` is not: an identity
belongs to a person, and a person exists before and independently of any
workspace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update

from app.db.models import FederatedIdentity, IdentityProvider
from app.repositories.base import BaseRepository


class FederatedIdentityRepository(BaseRepository[FederatedIdentity]):
    """Reads and writes for the `user_identities` table."""

    model = FederatedIdentity

    async def get_by_subject(
        self,
        *,
        provider: IdentityProvider,
        subject: str,
    ) -> FederatedIdentity | None:
        """The identity for an issuer's subject, if this account is known.

        The only correct way to resolve a federated login. Looking a person up
        by the email address in their token instead is the vulnerability this
        whole feature is arranged to avoid.
        """
        return await self._first(
            self._select().where(
                FederatedIdentity.provider == provider,
                FederatedIdentity.provider_subject == subject,
            )
        )

    async def get_for_user(
        self,
        *,
        user_id: uuid.UUID,
        provider: IdentityProvider,
    ) -> FederatedIdentity | None:
        """This account's identity at one issuer, if it has one.

        At most one row can match: `(user_id, provider)` is unique.
        """
        return await self._first(
            self._select().where(
                FederatedIdentity.user_id == user_id,
                FederatedIdentity.provider == provider,
            )
        )

    async def count_for_user(self, *, user_id: uuid.UUID) -> int:
        """How many issuers can open this account.

        Read before an unlink, to refuse one that would leave an account with no
        way in at all.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(FederatedIdentity)
            .where(FederatedIdentity.user_id == user_id)
        )
        return int(result.scalar_one())

    def create(
        self,
        *,
        user_id: uuid.UUID,
        provider: IdentityProvider,
        subject: str,
        now: datetime | None = None,
    ) -> FederatedIdentity:
        """Stage an identity for insertion.

        Deliberately not `async` and deliberately without a prior existence
        check. The uniqueness constraints are the guard, and asking first would
        only widen the window between the question and the write - which is the
        race two simultaneous first logins would drive straight through. The
        caller flushes and translates the integrity error.

        `now` stamps `last_login_at` when the identity is created *by* a login.
        Left as `None` when it is created by an explicit link, because linking
        an account is not signing in with it, and "connected but never used" is
        a state worth being able to see.
        """
        return self.add(
            FederatedIdentity(
                user_id=user_id,
                provider=provider,
                provider_subject=subject,
                last_login_at=now,
            )
        )

    async def stamp_login(self, *, identity_id: uuid.UUID, now: datetime) -> None:
        """Record that this identity was just used.

        One `UPDATE`, not a read-modify-write (ADR-039). Two sessions opened at
        once would otherwise race to write the timestamp each of them read.
        """
        await self._session.execute(
            update(FederatedIdentity)
            .where(FederatedIdentity.id == identity_id)
            .values(last_login_at=now),
            execution_options={"synchronize_session": False},
        )

    async def delete_for_user(
        self,
        *,
        user_id: uuid.UUID,
        provider: IdentityProvider,
    ) -> bool:
        """Detach an issuer from an account, reporting whether there was one.

        A single conditional `DELETE`, so two concurrent unlink requests cannot
        both believe they were the one that did it: whether a row was removed
        *is* the answer, the same shape `RefreshTokenStore.spend` uses.
        """
        result = await self._session.execute(
            delete(FederatedIdentity).where(
                FederatedIdentity.user_id == user_id,
                FederatedIdentity.provider == provider,
            ),
            execution_options={"synchronize_session": False},
        )
        return bool(cast("CursorResult[Any]", result).rowcount)
