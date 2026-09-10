"""The life of a workspace, from creation to tombstone.

Six operations, and the reason they live together is that five of them turn on
the same invariant: **an active workspace always has at least one active owner.**
A workspace with no owner cannot invite one, cannot change its own plan and
cannot be deleted by anybody who is in it; it is unrecoverable from the inside,
and the person who caused it never meant to.

The invariant is not enforceable by a constraint. It is a property of a *set* of
membership rows, and PostgreSQL has no way to say "at least one row in this
group satisfies a predicate" without a trigger or a materialised counter. So it
is enforced the other way round: every operation that could break it takes the
tenant row under ``FOR UPDATE`` first, and reads the owner set inside that lock
(``TenantRepository.lock`` argues the choice of row). Two owners leaving at the
same instant queue instead of racing, and the second one sees the state the
first one left rather than the state they both started from.

Three separations are load-bearing, and each one exists because collapsing it
would hand somebody authority they should not have.

**Removing a member is not deleting an account.** A membership is what one
workspace says about a person; an account is that person. A workspace
administrator can withdraw the first and can never touch the second - which is
`MembershipService`'s job, not this module's, and the reason those two live
apart.

**Deleting a workspace is not deleting its members.** The tombstone here reaches
the tenant and its memberships and nothing else. Everybody who was in it keeps
their account, their other workspaces and their sessions; a colleague at a
company that closed does not lose the login they use at a different one.

**Suspending is not deleting.** Suspension is the platform stopping service and
is reversible by the platform; deletion is the customer ending the relationship
and is not reversible at all. They are different states, different authorities
and different audit actions, and `TenantStatus.SUSPENDED` existing while nothing
could write it is what made that worth saying out loud.

What deletion does *not* do is erase data, and that is a decision rather than an
omission - see :meth:`WorkspaceService.delete`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.telemetry import observe_lifecycle_event
from app.db.models import Membership, MembershipStatus, Tenant, TenantRole, User
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.enums import TenantStatus
from app.repositories import (
    MembershipRepository,
    TenantRepository,
    UserMembershipRepository,
    UserRepository,
)
from app.repositories.tenant_repository import normalise_slug
from app.services.audit_service import AuditTrail
from app.services.subscription_service import bootstrap_default_subscription

logger = get_logger(__name__)

TENANT_SLUG_CONSTRAINT = "uq_tenants_slug"

# Stable machine-readable codes for the lifecycle conflicts a client has to be
# able to act on. Every one of them is a 409: the request was well-formed and
# the caller was allowed to make it, and the state of the workspace is what
# refused. A client shows a different screen for each, which is why they are
# codes and not a shared "conflict".
LAST_WORKSPACE_OWNER = "last_workspace_owner"
OWNERSHIP_TRANSFER_INVALID = "ownership_transfer_invalid"
WORKSPACE_ALREADY_SUSPENDED = "workspace_already_suspended"
WORKSPACE_NOT_SUSPENDED = "workspace_not_suspended"
WORKSPACE_DELETED = "workspace_deleted"
WORKSPACE_LIMIT_REACHED = "workspace_limit_reached"
WORKSPACE_CONFIRMATION_INVALID = "workspace_confirmation_invalid"


@dataclass(frozen=True, slots=True)
class WorkspaceCreation:
    """A new workspace and the ownership its creator was given."""

    tenant: Tenant
    membership: Membership


@dataclass(frozen=True, slots=True)
class OwnershipTransfer:
    """Both sides of a transfer, after it has happened."""

    tenant: Tenant
    previous_owner: Membership
    new_owner: Membership


class WorkspaceService:
    """Create, update, transfer, suspend, restore and delete workspaces.

    Deliberately **not** tenant-scoped at construction, unlike
    `MembershipService`. Two of these operations are platform actions on an
    arbitrary workspace and one creates the workspace it acts on, so there is no
    tenant to bind at construction time for at least half the surface.

    That puts the burden on the routes, and it is a burden they already carry:
    every tenant id reaching this service comes from an `ActiveWorkspace`, which
    is resolved from the signed token's `tid` and re-checked against a live
    membership on every request, or from a `PlatformStaffDep`, whose authority
    is deliberately not scoped to a workspace. **No method here takes a tenant
    id from a request body**, and none should be given one.

    Owns no transaction. The request-scoped session commits when the request
    succeeds, which matters here more than usual: an audit entry describing a
    transfer that rolled back is a record of something that did not happen.
    """

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings if settings is not None else get_settings()
        self._tenants = TenantRepository(session)
        self._users = UserRepository(session)
        self._memberships = UserMembershipRepository(session)

    # ------------------------------------------------------------- creation

    async def create(self, *, owner: User, name: str, slug: str) -> WorkspaceCreation:
        """Open a new workspace with this person as its owner.

        The operation registration always performed and nothing else could, and
        the absence is what stranded Google-first accounts: `_enrol` creates an
        account with no workspace on purpose - Google supplies no business name
        and a slug invented from a display name is a trap - and until now the
        only route that created a workspace also created an account, so such a
        person held a valid session, an empty workspace list and no way out
        except being invited somewhere (ADR-047). This is the way out.

        **Three rows or none.** The tenant, the owner membership and the
        subscription are one unit of work inside a savepoint. A workspace
        without its owner membership is the unrecoverable state this whole
        module exists to prevent, and creating one and then failing would
        manufacture it.

        The savepoint is what makes the slug race answerable. A failed statement
        aborts its transaction in PostgreSQL, so without one the conflict could
        not be returned - the session would already be poisoned. Only the slug
        constraint is translated; any other integrity error stays a 500 and
        therefore stays visible as a defect.
        """
        limit = self._settings.max_owned_workspaces_per_user
        owned = await self._memberships.list_owned_live_workspaces(owner.id)
        if len(owned) >= limit:
            raise ConflictError(
                f"This account already owns the maximum of {limit} workspaces.",
                error_code=WORKSPACE_LIMIT_REACHED,
                details={"limit": limit, "owned": len(owned)},
            )

        try:
            async with self._session.begin_nested():
                creation = await self._create(owner=owner, name=name, slug=slug)
        except IntegrityError as error:
            if TENANT_SLUG_CONSTRAINT in str(error.orig):
                observe_lifecycle_event(operation="workspace_create", outcome="conflict")
                raise ConflictError("That workspace address is already in use.") from error
            raise

        observe_lifecycle_event(operation="workspace_create", outcome="success")
        return creation

    async def _create(self, *, owner: User, name: str, slug: str) -> WorkspaceCreation:
        tenant = await self._tenants.create(name=name, slug=slug)
        # Identifiers are assigned on flush, and the membership needs both.
        await self._session.flush()

        memberships = MembershipRepository(self._session, tenant_id=tenant.id)
        membership = await memberships.add_member(user_id=owner.id, role=TenantRole.TENANT_OWNER)
        await self._session.flush()

        await bootstrap_default_subscription(
            self._session,
            tenant_id=tenant.id,
            settings=self._settings,
        )

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.WORKSPACE_CREATED,
            actor=owner,
            actor_kind=AuditActorKind.USER,
            target_type="tenant",
            target_id=tenant.id,
            target_label=tenant.slug,
            meta={"slug": tenant.slug},
        )
        logger.info(
            "workspace.created",
            extra={
                "event": "workspace.created",
                "tenant_id": str(tenant.id),
                "user_id": str(owner.id),
            },
        )
        return WorkspaceCreation(tenant=tenant, membership=membership)

    # --------------------------------------------------------------- update

    async def update(
        self,
        *,
        tenant: Tenant,
        actor: User,
        actor_role: TenantRole,
        name: str | None = None,
        slug: str | None = None,
    ) -> Tenant:
        """Change a workspace's name, and - for an owner only - its address.

        The two fields are split by authority rather than lumped together, and
        the split is the point. A name is a label: an administrator renaming the
        workspace inconveniences nobody and is not audited. A **slug is an
        identifier** - it is what invitation links, saved bookmarks and support
        tickets name - so changing it silently redirects every one of those and
        frees the old address for somebody else to take. That is an owner's
        decision, and it is recorded.
        """
        if actor_role not in (TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN):
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        if name is None and slug is None:
            raise ValidationError("Nothing to update.")

        changed: dict[str, str] = {}
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise ValidationError("A workspace name cannot be blank.")
            tenant.name = cleaned

        if slug is not None:
            if actor_role is not TenantRole.TENANT_OWNER:
                raise PermissionDeniedError("Only a workspace owner can change its address.")
            normalised = normalise_slug(slug)
            if normalised != tenant.slug:
                existing = await self._tenants.get_by_slug(normalised)
                if existing is not None:
                    raise ConflictError("That workspace address is already in use.")
                changed = {"previous_slug": tenant.slug, "slug": normalised}
                tenant.slug = normalised

        await self._session.flush()

        if changed:
            # Only the address change leaves a trail. Auditing a rename would
            # add rows nobody filters on and bury the one entry that matters.
            AuditTrail(self._session, tenant_id=tenant.id).record(
                AuditAction.WORKSPACE_UPDATED,
                actor=actor,
                actor_kind=AuditActorKind.USER,
                target_type="tenant",
                target_id=tenant.id,
                target_label=tenant.slug,
                meta=changed,
            )
        logger.info(
            "workspace.updated",
            extra={
                "event": "workspace.updated",
                "tenant_id": str(tenant.id),
                "user_id": str(actor.id),
                "address_changed": bool(changed),
            },
        )
        return tenant

    # ------------------------------------------------------------ ownership

    async def transfer_ownership(
        self,
        *,
        tenant_id: uuid.UUID,
        actor: User,
        target_user_id: uuid.UUID,
    ) -> OwnershipTransfer:
        """Hand ownership to another active member, stepping down in the process.

        **What transfer means here.** The target becomes `TENANT_OWNER` and the
        caller becomes `TENANT_ADMIN`, atomically. It is not "add an owner": the
        membership model supports several owners at once and this method does
        not change that - a workspace that already has three owners still has
        three afterwards - but a route that merely *promoted* somebody would be
        a role-granting endpoint wearing a transfer's name, and the thing people
        press "transfer ownership" for is handing the workspace over on their
        way out.

        The caller keeps administrator access rather than being ejected, because
        the alternative surprises people: somebody handing over ownership before
        going on leave should not lose the ability to do their job. Leaving
        entirely is `DELETE /workspace/members/{id}` and is a separate decision.

        **Under the tenant lock**, because the invariant is about the owner set:
        two owners transferring to each other at the same moment would otherwise
        both step down against a snapshot showing the other still in place, and
        the workspace would end with none.
        """
        tenant = await self._lock_live_workspace(tenant_id)
        memberships = MembershipRepository(self._session, tenant_id=tenant.id)

        actor_membership = await memberships.get_for_user(actor.id)
        if actor_membership is None or actor_membership.role is not TenantRole.TENANT_OWNER:
            raise PermissionDeniedError("Only a workspace owner can transfer ownership.")

        if target_user_id == actor.id:
            raise ConflictError(
                "Ownership is already held by this account.",
                error_code=OWNERSHIP_TRANSFER_INVALID,
            )

        target = await memberships.get_for_user(target_user_id)
        if target is None:
            # Not a 404 about the *user*: whether that account exists elsewhere
            # on the platform is not something this endpoint discloses.
            raise ConflictError(
                "That person is not an active member of this workspace.",
                error_code=OWNERSHIP_TRANSFER_INVALID,
            )

        target_user = await self._users.get_by_id(target_user_id)
        if target_user is None or not target_user.is_active:
            # A disabled account cannot be handed a workspace: it would satisfy
            # the "has an owner" invariant while being unable to act on it,
            # which is the unrecoverable state wearing a disguise.
            raise ConflictError(
                "That account cannot take ownership.",
                error_code=OWNERSHIP_TRANSFER_INVALID,
            )

        target.role = TenantRole.TENANT_OWNER
        actor_membership.role = TenantRole.TENANT_ADMIN
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.WORKSPACE_OWNERSHIP_TRANSFERRED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="tenant",
            target_id=tenant.id,
            target_label=tenant.slug,
            # Both parties by address as well as by id, so the entry stays
            # readable after either account is closed.
            meta={
                "from_user_id": str(actor.id),
                "from_user": actor.email,
                "to_user_id": str(target_user.id),
                "to_user": target_user.email,
            },
        )
        observe_lifecycle_event(operation="ownership_transfer", outcome="success")
        logger.info(
            "workspace.ownership_transferred",
            extra={
                "event": "workspace.ownership_transferred",
                "tenant_id": str(tenant.id),
                "user_id": str(actor.id),
            },
        )
        return OwnershipTransfer(
            tenant=tenant,
            previous_owner=actor_membership,
            new_owner=target,
        )

    # ------------------------------------------------------------- deletion

    async def delete(
        self,
        *,
        tenant_id: uuid.UUID,
        actor: User,
        confirmation: str,
    ) -> Tenant:
        """Tombstone a workspace at its owner's request.

        **Soft, not hard, and the foreign keys are the argument.** Twenty-nine
        tables reference `tenants` and twenty-eight of them cascade, so a
        ``DELETE FROM tenants`` would take the invoices and the payments with
        it. Destroying financial records to satisfy a button is not a trade
        anybody would authorise if asked, and nobody would be asked - it would
        simply happen. `deleted_at` keeps every row addressable for the
        accounting, tax and dispute questions that arrive after a customer
        leaves, and `Tenant.is_active` already treats a tombstoned workspace as
        dead, so access stops at the same instant either way.

        **Access ends immediately.** `get_active_workspace` refuses a tenant
        that is not active on the very next request, and the memberships are
        revoked here as well - belt and braces, and it is what makes the
        workspace disappear from everybody's switcher rather than lingering as
        an entry that 403s.

        **Nobody's account is touched.** Not the owner's, not any member's.
        Sessions are left alone deliberately: a colleague signed in to three
        workspaces should not be signed out of the other two because this one
        closed, and there is nothing to revoke - authorization re-reads the
        tenant on every request, so the tokens simply stop opening this door.

        What is *not* implemented is a scheduled purge. There is no deferred-job
        infrastructure in this repository that could run one reliably, and a
        method that claimed to schedule erasure while nothing ran is worse than
        one that does not claim it. Erasure remains an operator runbook step;
        see docs/RUNBOOK.md.

        The confirmation is checked here rather than in the schema, because the
        value it must equal is the workspace's own slug and a schema cannot see
        the workspace. A frontend modal is not a control: this route is
        reachable with curl.
        """
        tenant = await self._lock_live_workspace(tenant_id)

        if confirmation.strip().lower() != tenant.slug:
            # Same shape whichever way it is wrong, and never echoing what was
            # sent. It is not a secret - the caller can read the slug from
            # `/auth/me` - but reflecting caller input into an error message is
            # a habit worth not having.
            raise ConflictError(
                "The confirmation does not match this workspace's address.",
                error_code=WORKSPACE_CONFIRMATION_INVALID,
            )

        memberships = MembershipRepository(self._session, tenant_id=tenant.id)
        actor_membership = await memberships.get_for_user(actor.id)
        if actor_membership is None or actor_membership.role is not TenantRole.TENANT_OWNER:
            raise PermissionDeniedError("Only a workspace owner can delete it.")

        moment = datetime.now(UTC)
        tenant.deleted_at = moment
        for membership in await memberships.list_members(include_revoked=False):
            membership.status = MembershipStatus.REVOKED
            membership.revoked_at = moment
            membership.revoked_by_id = actor.id
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.WORKSPACE_DELETED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="tenant",
            target_id=tenant.id,
            # The slug, because `audit_logs.tenant_id` is `SET NULL` on delete
            # and this entry has to stay readable if the row ever does go.
            target_label=tenant.slug,
            meta={"slug": tenant.slug},
        )
        observe_lifecycle_event(operation="workspace_delete", outcome="success")
        logger.info(
            "workspace.deleted",
            extra={
                "event": "workspace.deleted",
                "tenant_id": str(tenant.id),
                "user_id": str(actor.id),
            },
        )
        return tenant

    # ----------------------------------------------------- platform control

    async def suspend(
        self,
        *,
        tenant_id: uuid.UUID,
        actor: User,
        reason: str | None = None,
    ) -> Tenant:
        """Stop serving a workspace, reversibly. Platform staff only.

        `TenantStatus.SUSPENDED` has existed since the first tenancy migration
        and nothing has ever written it, so the state was declared and
        unreachable. This is what reaches it.

        The customer's data, memberships, subscription and number claims are all
        left exactly as they are. Suspension says "we have stopped serving this
        workspace", which is what an operator needs during an abuse
        investigation or a payment dispute, and it says nothing else - undoing
        it with :meth:`restore` returns the workspace to precisely the state it
        was in.

        Sessions are deliberately **not** revoked. A membership in a suspended
        workspace stops authorising on the next request because
        `get_active_workspace` reads `Tenant.is_active`, so nothing has to
        expire - and bumping `token_version` would sign those people out of
        every *other* workspace they belong to, which is a punishment aimed at
        the wrong people. The account remains usable; only this workspace stops.
        """
        tenant = await self._require_workspace(tenant_id)
        if tenant.deleted_at is not None:
            raise ConflictError(
                "That workspace has been deleted.",
                error_code=WORKSPACE_DELETED,
            )
        if tenant.status is TenantStatus.SUSPENDED:
            raise ConflictError(
                "That workspace is already suspended.",
                error_code=WORKSPACE_ALREADY_SUSPENDED,
            )

        tenant.status = TenantStatus.SUSPENDED
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.WORKSPACE_SUSPENDED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="tenant",
            target_id=tenant.id,
            target_label=tenant.slug,
            # An operator's own words about why. Bounded by the schema, and it
            # is the one field here a person types - so it is recorded as given
            # and never interpreted.
            meta={"reason": reason} if reason else None,
        )
        observe_lifecycle_event(operation="workspace_suspend", outcome="success")
        logger.info(
            "workspace.suspended",
            extra={
                "event": "workspace.suspended",
                "tenant_id": str(tenant.id),
                "actor_id": str(actor.id),
            },
        )
        return tenant

    async def restore(self, *, tenant_id: uuid.UUID, actor: User) -> Tenant:
        """Return a suspended workspace to service. Platform staff only.

        The exact inverse of :meth:`suspend` and nothing more. It does not
        resurrect anything: no session is reinstated, because none was revoked;
        no membership is restored, because none was withdrawn. People who could
        use the workspace before can use it again on their next request, and
        anybody who lost access for a different reason - removed by an
        administrator, disabled at the platform - stays without it.

        A **deleted** workspace is not restorable here. Deletion is the
        customer's decision and this is platform tooling; undoing it would let
        staff reopen a business relationship the customer ended. Reversing a
        deletion made in error is an operator action against the database, with
        the deliberation that implies (docs/RUNBOOK.md).
        """
        tenant = await self._require_workspace(tenant_id)
        if tenant.deleted_at is not None:
            raise ConflictError(
                "That workspace has been deleted and cannot be restored here.",
                error_code=WORKSPACE_DELETED,
            )
        if tenant.status is not TenantStatus.SUSPENDED:
            raise ConflictError(
                "That workspace is not suspended.",
                error_code=WORKSPACE_NOT_SUSPENDED,
            )

        tenant.status = TenantStatus.ACTIVE
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.WORKSPACE_RESTORED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="tenant",
            target_id=tenant.id,
            target_label=tenant.slug,
        )
        observe_lifecycle_event(operation="workspace_restore", outcome="success")
        logger.info(
            "workspace.restored",
            extra={
                "event": "workspace.restored",
                "tenant_id": str(tenant.id),
                "actor_id": str(actor.id),
            },
        )
        return tenant

    # ------------------------------------------------------------- internals

    async def _require_workspace(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("No workspace matches that identifier.")
        return tenant

    async def _lock_live_workspace(self, tenant_id: uuid.UUID) -> Tenant:
        """The workspace, locked, provided it is still alive.

        Every caller of this is about to read the owner set and act on it, so
        the lock is taken before the read rather than around the write. The
        liveness check is *inside* the lock for the same reason: a workspace
        deleted by the request that is currently holding this row must not be
        transferred or deleted again by the one waiting behind it.
        """
        tenant = await self._tenants.lock(tenant_id)
        if tenant is None:
            raise NotFoundError("No workspace matches that identifier.")
        if tenant.deleted_at is not None:
            raise ConflictError(
                "That workspace has been deleted.",
                error_code=WORKSPACE_DELETED,
            )
        return tenant
