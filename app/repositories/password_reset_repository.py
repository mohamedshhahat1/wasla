"""Password reset tokens: created, looked up by hash, and spent atomically."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.password_reset import PasswordResetToken


class PasswordResetTokenRepository:
    """Every query the reset flow makes. Deliberately unscoped: a reset acts
    on a global identity, not a workspace, so there is no tenant to scope by
    - the same reasoning as `UserRepository`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> PasswordResetToken:
        token = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_token_hash(self, token_hash: str) -> PasswordResetToken | None:
        statement = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        return (await self._session.execute(statement)).scalars().first()

    async def consume(self, *, token_id: uuid.UUID, now: datetime) -> bool:
        """Spend a token exactly once, however many confirmations race.

        One ``UPDATE ... WHERE consumed_at IS NULL RETURNING`` rather than a
        read followed by a write - the ADR-039 shape. Two requests carrying
        the same token both pass the earlier read; exactly one wins this
        write, and losing is the refusal.
        """
        statement = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.id == token_id,
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.superseded_at.is_(None),
            )
            .values(consumed_at=now)
            .returning(PasswordResetToken.id)
        )
        return (await self._session.execute(statement)).first() is not None

    async def supersede_outstanding(self, *, user_id: uuid.UUID, now: datetime) -> int:
        """End every live token this account holds.

        Called when a new token is issued - so repeated requests leave one
        live token rather than a growing pile - and again when a reset
        succeeds, so an older link found later opens nothing.
        """
        statement = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return int(result.rowcount or 0)
