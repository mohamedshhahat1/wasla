"""Workspace invitations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    generate_invitation_token,
    hash_invitation_token,
    hash_password,
    validate_password_strength,
)
from app.db.models import InvitationStatus, Membership, Tenant, TenantInvitation, TenantRole, User
from app.repositories import (
    InvitationRepository,
    InvitationTokenRepository,
    MembershipRepository,
    TenantRepository,
    UserRepository,
)

logger = get_logger(__name__)

INVITATION_TTL_DAYS: Final = 7
# Unknown, spent, revoked and expired invitations all answer this way, so the
# endpoint cannot be used to probe which tokens once existed.
INVALID_INVITATION: Final = "That invitation is not valid."


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    """What acceptance produced: an account and its new membership."""

    user: User
    membership: Membership
    tenant: Tenant


class InvitationService:
    """Issuing, revoking and accepting workspace invitations.

    Accepting prepares the account and the membership but mints no session.
    Signing in stays a separate step, so a leaked invitation link cannot by
    itself hand out a live session.
    """

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)
        self._tokens = InvitationTokenRepository(session)

    async def issue(
        self,
        *,
        tenant_id: uuid.UUID,
        inviter: User,
        inviter_role: TenantRole,
        email: str,
        role: TenantRole,
    ) -> tuple[TenantInvitation, str]:
        """Create an invitation and return it with its one-time token."""
        if role is TenantRole.TENANT_OWNER and inviter_role is not TenantRole.TENANT_OWNER:
            # Otherwise the boundary between admin and owner is decorative: any
            # administrator could mint themselves a peer with full authority.
            raise PermissionDeniedError("Only a workspace owner can invite another owner.")

        existing = await self._users.get_by_email(email)
        if existing is not None:
            memberships = MembershipRepository(self._session, tenant_id=tenant_id)
            if await memberships.get_for_user(existing.id) is not None:
                raise ConflictError("That person is already a member of this workspace.")

        raw_token, token_hash = generate_invitation_token()
        invitations = InvitationRepository(self._session, tenant_id=tenant_id)
        invitation = await invitations.create(
            email=email,
            role=role,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS),
            invited_by_id=inviter.id,
        )
        await self._session.flush()

        logger.info(
            "invitation.issued",
            extra={
                "event": "invitation.issued",
                "tenant_id": str(tenant_id),
                "user_id": str(inviter.id),
                "role": role.value,
            },
        )
        return invitation, raw_token

    async def list_pending(self, *, tenant_id: uuid.UUID) -> list[TenantInvitation]:
        invitations = InvitationRepository(self._session, tenant_id=tenant_id)
        return await invitations.list_pending()

    async def revoke(
        self,
        *,
        tenant_id: uuid.UUID,
        invitation_id: uuid.UUID,
    ) -> TenantInvitation:
        invitations = InvitationRepository(self._session, tenant_id=tenant_id)
        invitation = await invitations.require_by_id(invitation_id)
        if invitation.status is not InvitationStatus.PENDING:
            raise ConflictError("That invitation is no longer pending.")
        invitation.status = InvitationStatus.REVOKED
        await self._session.flush()
        logger.info(
            "invitation.revoked",
            extra={
                "event": "invitation.revoked",
                "tenant_id": str(tenant_id),
            },
        )
        return invitation

    async def accept(
        self,
        *,
        raw_token: str,
        password: str | None = None,
        full_name: str | None = None,
    ) -> AcceptedInvitation:
        """Turn a valid invitation into a membership.

        The lookup is by hash, so a stolen database yields no usable
        invitation.
        """
        invitation = await self._tokens.get_by_token_hash(hash_invitation_token(raw_token))
        now = datetime.now(UTC)
        if invitation is None or not invitation.is_open(now=now):
            raise AuthenticationError(INVALID_INVITATION)

        tenant = await self._tenants.get_by_id(invitation.tenant_id)
        if tenant is None or not tenant.is_active:
            raise PermissionDeniedError("That workspace is not available.")

        user = await self._users.get_by_email(invitation.email)
        if user is None:
            if password is None:
                raise ValidationError("A password is required to create the account.")
            validate_password_strength(password)
            user = await self._users.create(
                email=invitation.email,
                full_name=full_name,
                hashed_password=hash_password(password),
            )
            await self._session.flush()
        elif user.hashed_password is None and password is not None:
            # An account created by an earlier invitation that was never
            # completed: this is where it gets a password.
            validate_password_strength(password)
            user.hashed_password = hash_password(password)

        memberships = MembershipRepository(self._session, tenant_id=invitation.tenant_id)
        membership = await memberships.get_for_user(user.id)
        if membership is None:
            membership = await memberships.add_member(user_id=user.id, role=invitation.role)

        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now
        await self._session.flush()

        logger.info(
            "invitation.accepted",
            extra={
                "event": "invitation.accepted",
                "tenant_id": str(invitation.tenant_id),
                "user_id": str(user.id),
            },
        )
        return AcceptedInvitation(user=user, membership=membership, tenant=tenant)
