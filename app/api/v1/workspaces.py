"""Workspace lifecycle endpoints.

Two prefixes, and the split is about what the request is scoped to rather than
about tidiness.

``POST /workspaces`` is **not** workspace-scoped. It is how a workspace comes
into existence, so there is no `tid` on the token and no membership to check;
the only authority it needs is a verified account. That is why it is plural and
alone.

``/workspace`` is the workspace the caller's token already names, resolved by
`ActiveWorkspaceDep` from the signed `tid` and re-checked against a live
membership on every request. Nothing on this router takes a tenant id from a
request body, so a forged one has nowhere to go - which is the property that
makes the whole tenancy model hold, and it is preserved here by having no
parameter that could carry it.

Suspension and restoration are absent from both, deliberately. They are platform
authority over somebody else's workspace, so they live on the platform router
with the rest of it.

**Rate limits are declared per route here rather than on the router**, which is
the rule `app/api/v1/__init__.py` states for exactly this shape. The workspace
limiter counts by workspace and resolves `ActiveWorkspaceDep` to find one, so
attaching it to the whole router would make creating a workspace require a
workspace - and the first request of Google-first onboarding would answer "No
workspace is selected for this session", which is the dead end this router
exists to close. The scoped routes carry it individually; creation carries the
per-address limit instead.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    TenantAdminDep,
    TenantOwnerDep,
    VerifiedUserDep,
    WorkspaceServiceDep,
)
from app.api.rate_limits import AuthRateLimit, WorkspaceRateLimit
from app.api.route import CommittingRoute
from app.db.models import Tenant
from app.schemas.workspace import (
    OwnershipTransferRequest,
    OwnershipTransferResponse,
    WorkspaceCreatedResponse,
    WorkspaceCreateRequest,
    WorkspaceDeleteRequest,
    WorkspaceRead,
    WorkspaceUpdateRequest,
)

router = APIRouter(route_class=CommittingRoute, tags=["Workspace"])


def _read(tenant: Tenant) -> WorkspaceRead:
    return WorkspaceRead(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        is_active=tenant.is_active,
    )


@router.post(
    "/workspaces",
    response_model=WorkspaceCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace and become its owner",
    responses={
        409: {"description": "The address is taken, or this account owns too many."},
    },
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: VerifiedUserDep,
    service: WorkspaceServiceDep,
    # Counted per client address, like the rest of the unscoped authenticated
    # surface. The workspace-scoped limiter cannot be used here: it counts by
    # workspace, and this is the request that creates one.
    limit: AuthRateLimit,
) -> WorkspaceCreatedResponse:
    """Open a new workspace. Any verified account may, up to a configured ceiling.

    **This is the route that unblocks Google-first onboarding.** Signing in with
    Google creates an account and no workspace - Google supplies no business
    name, and a slug invented from a display name is a trap, because
    `SLUG_PATTERN` is strict ASCII and a great many real names are not
    (ADR-047). Until now the only code that created a workspace also created an
    account, so such a person held a valid session, an empty workspace list and
    no way forward except waiting to be invited somewhere. Now the client asks
    them for a business name and calls this.

    It is not only for Google accounts. A person who registered with a password
    can open a second workspace here too, which is what the multi-workspace
    membership model has always described and no route has ever offered.

    `VerifiedUserDep`, not `CurrentUserDep`: creating a workspace is a material
    business action and the shared rule is that those wait for a proven address.
    A Google account whose domain Google is authoritative for is already
    verified at enrolment and passes straight through.

    The response carries no token. Selecting the new workspace is
    `POST /auth/workspace`, which re-checks membership and mints an access token
    with the new `tid` - one issuance path, which is what stops a second and
    subtly different one from existing (ADR-058).
    """
    created = await service.create(
        owner=current_user.user,
        name=payload.name,
        slug=payload.slug,
    )
    return WorkspaceCreatedResponse(
        workspace=_read(created.tenant),
        role=created.membership.role,
    )


@router.get(
    "/workspace",
    response_model=WorkspaceRead,
    summary="Describe the workspace this session is using",
)
async def read_workspace(
    workspace: ActiveWorkspaceDep,
    limit: WorkspaceRateLimit,
) -> WorkspaceRead:
    """Open to any member. Knowing which workspace you are in is not privileged."""
    return _read(workspace.tenant)


@router.patch(
    "/workspace",
    response_model=WorkspaceRead,
    summary="Change this workspace's name or address",
    responses={
        403: {"description": "Only an owner may change the address."},
        409: {"description": "That address is already in use."},
    },
)
async def update_workspace(
    payload: WorkspaceUpdateRequest,
    workspace: TenantAdminDep,
    service: WorkspaceServiceDep,
    limit: WorkspaceRateLimit,
) -> WorkspaceRead:
    """Administrators may rename; only an owner may change the address.

    The dependency admits owners and administrators, and the service refuses an
    administrator who tries to change the slug. That split is not arbitrary: a
    name is a label, while the address is what invitation links, bookmarks and
    support tickets name, so changing it redirects all of them and frees the old
    one for somebody else to claim.
    """
    tenant = await service.update(
        tenant=workspace.tenant,
        actor=workspace.user,
        actor_role=workspace.role,
        name=payload.name,
        slug=payload.slug,
    )
    return _read(tenant)


@router.post(
    "/workspace/ownership",
    response_model=OwnershipTransferResponse,
    summary="Hand ownership to another member",
    responses={
        403: {"description": "Only an owner may transfer ownership."},
        409: {"description": "The target is not an eligible active member."},
    },
)
async def transfer_ownership(
    payload: OwnershipTransferRequest,
    workspace: TenantOwnerDep,
    service: WorkspaceServiceDep,
    limit: WorkspaceRateLimit,
) -> OwnershipTransferResponse:
    """The target becomes owner; the caller becomes an administrator.

    A transfer, not a promotion. The membership model supports several owners at
    once and this does not change that - a workspace with three owners still has
    three - but the thing people press this for is handing the workspace over,
    and a route that only added an owner would be a role-granting endpoint under
    a misleading name.

    The caller keeps administrator access rather than being ejected: somebody
    transferring ownership before going on leave should not lose the ability to
    do their job. Leaving entirely is `DELETE /workspace/members/{user_id}`.

    Serialised on the workspace row, so two owners transferring at the same
    moment cannot both step down against a snapshot in which the other is still
    an owner.
    """
    transfer = await service.transfer_ownership(
        tenant_id=workspace.tenant.id,
        actor=workspace.user,
        target_user_id=payload.user_id,
    )
    return OwnershipTransferResponse(
        workspace=_read(transfer.tenant),
        previous_owner_id=transfer.previous_owner.user_id,
        previous_owner_role=transfer.previous_owner.role,
        new_owner_id=transfer.new_owner.user_id,
        new_owner_role=transfer.new_owner.role,
    )


@router.delete(
    "/workspace",
    response_model=WorkspaceRead,
    summary="Close this workspace",
    responses={
        403: {"description": "Only an owner may delete a workspace."},
        409: {"description": "The confirmation does not match, or it is already deleted."},
    },
)
async def delete_workspace(
    payload: WorkspaceDeleteRequest,
    workspace: TenantOwnerDep,
    service: WorkspaceServiceDep,
    limit: WorkspaceRateLimit,
) -> WorkspaceRead:
    """Owner only, and the address must be typed to confirm.

    **What it does.** The workspace is tombstoned and every membership in it is
    withdrawn. Access ends on the next request for everybody - authorization
    re-reads the tenant each time - and the workspace disappears from their
    workspace switcher rather than lingering as an entry that refuses.

    **What it does not do.** It deletes nobody's account, including the owner's,
    and signs nobody out of anything else. It also does not erase data:
    twenty-eight tables cascade from `tenants`, invoices and payments among
    them, so a hard delete would destroy financial records to satisfy a button.
    The rows stay addressable for the accounting and dispute questions that
    arrive after a customer leaves.

    **What is not implemented** is a scheduled purge, and it is not pretended:
    there is no deferred-job infrastructure here that could run one reliably.
    Erasure is an operator step, in docs/RUNBOOK.md.

    Restoring a deleted workspace is not an API operation. Suspension is the
    reversible state and belongs to platform staff; this one is the customer
    ending the relationship.
    """
    tenant = await service.delete(
        tenant_id=workspace.tenant.id,
        actor=workspace.user,
        confirmation=payload.confirmation,
    )
    return _read(tenant)
