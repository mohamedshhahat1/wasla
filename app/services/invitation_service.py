"""Workspace invitations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
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
from app.db.models import (
    InvitationStatus,
    Membership,
    MembershipStatus,
    Tenant,
    TenantInvitation,
    TenantRole,
    User,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.repositories import (
    InvitationRepository,
    InvitationTokenRepository,
    MembershipRepository,
    TenantRepository,
    UserRepository,
)
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate

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

    Invitations and revocation (ADR-038) interact in exactly one place, and it
    is deliberate that there is only one. Revoking a membership leaves any
    invitation alone: there cannot be a pending one for an active member, since
    `issue` refuses that, and an invitation issued *after* a removal is how
    somebody is asked back. Acceptance then reactivates the existing membership
    row rather than adding a second one.

    Delivery goes through the outbox on the caller's session (ADR-042), so an
    invitation and the email announcing it commit together or not at all. This
    service never reaches a provider.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        # Defaulted rather than required so existing construction sites keep
        # working; the request-scoped provider passes the real settings in.
        self._settings = settings if settings is not None else get_settings()
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)
        self._tokens = InvitationTokenRepository(session)
        self._outbox = EmailOutbox(session, self._settings)

    async def issue(
        self,
        *,
        tenant_id: uuid.UUID,
        inviter: User,
        inviter_role: TenantRole,
        email: str,
        role: TenantRole,
    ) -> tuple[TenantInvitation, str]:
        """Create an invitation, queue its email, and return the one-time token.

        The email is queued on this session rather than sent, so a caller whose
        transaction rolls back later - a seat check, an integrity error - does
        not leave an invitation announced to somebody who cannot accept it.
        """
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

        tenant = await self._tenants.get_by_id(tenant_id)
        # Keyed to the invitation row, so re-issuing against the same row cannot
        # produce a second message however often the request is repeated. The
        # workspace name is read from the tenant row rather than taken from the
        # request, and it is the only caller-influenced value in the template.
        await self._outbox.enqueue(
            template=EmailTemplate.WORKSPACE_INVITATION,
            recipient=invitation.email,
            idempotency_key=f"invitation:{invitation.id}",
            context={
                "workspace_name": tenant.name if tenant is not None else "a workspace",
                "token": raw_token,
            },
            tenant_id=tenant_id,
            user_id=existing.id if existing is not None else None,
        )

        AuditTrail(self._session, tenant_id=tenant_id).record(
            AuditAction.MEMBER_INVITED,
            actor=inviter,
            # Deliberately as a USER even when the inviter holds a platform
            # role: they are acting inside a workspace they belong to, and the
            # log should say so.
            actor_kind=AuditActorKind.USER,
            target_type="invitation",
            target_id=invitation.id,
            target_label=email,
            meta={"role": role.value},
        )

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
        # Deliberately no email. Revoking an invitation the person may never
        # have seen, to a workspace they were never in, would be telling a
        # stranger they had been un-invited - and if the outbox row is still
        # pending, revocation does not unsend it: the token it carries is dead
        # either way, which is what makes an undelivered revocation harmless.
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
        # Deliberately the read that sees revoked rows. Somebody removed from a
        # workspace and invited back has a membership already, and the unique
        # constraint on `(user_id, tenant_id)` means a second one cannot be
        # inserted - so an invitation that ignored the old row would fail at
        # flush with an integrity error instead of readmitting them (ADR-038).
        membership = await memberships.get_any_for_user(user.id)
        if membership is None:
            membership = await memberships.add_member(user_id=user.id, role=invitation.role)
        elif not membership.is_active:
            # Readmission. The invitation's role wins over the one they held
            # before: whoever issued it decided what they are coming back as.
            membership.status = MembershipStatus.ACTIVE
            membership.role = invitation.role
            membership.revoked_at = None
            membership.revoked_by_id = None

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
