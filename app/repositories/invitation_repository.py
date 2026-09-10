"""Tenant invitation data access."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, update

from app.core.exceptions import ConflictError
from app.db.models import InvitationStatus, TenantInvitation, TenantRole
from app.repositories.base import BaseRepository, TenantScopedRepository
from app.repositories.user_repository import normalise_email


class InvitationRepository(TenantScopedRepository[TenantInvitation]):
    """Invitations belonging to one workspace."""

    model = TenantInvitation

    def _tenant_filter(self) -> ColumnElement[bool]:
        return TenantInvitation.tenant_id == self.tenant_id

    async def get_by_id(self, invitation_id: uuid.UUID) -> TenantInvitation | None:
        return await self._first(self._select().where(TenantInvitation.id == invitation_id))

    async def require_by_id(self, invitation_id: uuid.UUID) -> TenantInvitation:
        return await self._require(self._select().where(TenantInvitation.id == invitation_id))

    async def get_pending_for_email(self, email: str) -> TenantInvitation | None:
        statement = self._select().where(
            TenantInvitation.email == normalise_email(email),
            TenantInvitation.status == InvitationStatus.PENDING,
        )
        return await self._first(statement)

    async def list_pending(self, *, limit: int = 50) -> list[TenantInvitation]:
        statement = (
            self._select()
            .where(TenantInvitation.status == InvitationStatus.PENDING)
            .order_by(TenantInvitation.created_at.desc())
            .limit(limit)
        )
        return await self._all(statement)

    async def revoke_pending(self, invitation_id: uuid.UUID) -> TenantInvitation | None:
        """Atomically revoke only a still-pending invitation."""
        statement = (
            update(TenantInvitation)
            .where(
                self._tenant_filter(),
                TenantInvitation.id == invitation_id,
                TenantInvitation.status == InvitationStatus.PENDING,
            )
            .values(status=InvitationStatus.REVOKED)
            .returning(TenantInvitation)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        role: TenantRole,
        token_hash: str,
        expires_at: datetime,
        invited_by_id: uuid.UUID | None = None,
    ) -> TenantInvitation:
        """Stage one pending invitation for an address.

        The uniqueness rule lives here rather than in a partial unique index,
        which Phase 1 deliberately left out of the schema.
        """
        normalised = normalise_email(email)
        if await self.get_pending_for_email(normalised) is not None:
            raise ConflictError("That address already has a pending invitation.")

        invitation = TenantInvitation(
            # From the repository's fixed scope, never from request input.
            tenant_id=self.tenant_id,
            email=normalised,
            role=role,
            status=InvitationStatus.PENDING,
            token_hash=token_hash,
            expires_at=expires_at,
            invited_by_id=invited_by_id,
        )
        return self.add(invitation)


class InvitationTokenRepository(BaseRepository[TenantInvitation]):
    """Resolution of one invitation by its token hash, across all workspaces.

    Acceptance happens before any workspace is known: the token is all the
    caller has, so this read cannot be tenant-scoped. It is safe because the
    token is unguessable and stored only as a hash, which makes holding it the
    authorization. This class contains exactly one query, and it matches on
    nothing but that hash.
    """

    model = TenantInvitation

    async def get_by_token_hash(self, token_hash: str) -> TenantInvitation | None:
        return await self._first(self._select().where(TenantInvitation.token_hash == token_hash))

    async def claim(
        self,
        *,
        token_hash: str,
        now: datetime,
    ) -> TenantInvitation | None:
        """Atomically spend one unexpired invitation token.

        The conditional update is the claim. Concurrent callers cannot both
        change ``pending`` to ``accepted``; PostgreSQL makes the loser wait for
        the winner and then re-evaluate the predicate against the committed
        status.
        """
        statement = (
            update(TenantInvitation)
            .where(
                TenantInvitation.token_hash == token_hash,
                TenantInvitation.status == InvitationStatus.PENDING,
                TenantInvitation.expires_at > now,
            )
            .values(status=InvitationStatus.ACCEPTED, accepted_at=now)
            .returning(TenantInvitation)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()
