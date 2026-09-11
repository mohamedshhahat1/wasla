"""Platform administration endpoints.

Behind platform authority, which is a different authority from every other
route in this API: it is a property of the user, not of a membership. Owning a
workspace grants nothing here, and holding a platform role grants nothing
*inside* a workspace - a platform administrator reading these figures still
cannot open a customer's inbox.

**Two platform roles, and they are no longer the same thing.** Nine routes take
`PlatformStaffDep`: an owner or an admin, because reading the platform's
figures, settling an invoice and suspending a workspace are the job. The two
that end an account's authority - `disable` and `delete` - take
`PlatformAccountTargetDep`, which additionally asks what the *target* is and
refuses an admin acting on a platform owner. Until that existed the pair were
identical over HTTP and the lesser could permanently tombstone the greater
(AUTHZ-01). `enable` stays on `PlatformStaffDep` and the route says why.

Reads, plus a short list of writes that each needed an argument to be here.

Recording a payment against an invoice asserts that somebody has seen money
arrive, and a customer able to make that assertion about their own invoice pays
nothing. Disabling, enabling and deleting an account act on a *global* identity,
which is why they are platform authority and not a workspace administrator's.
And suspending or restoring a workspace stops or resumes service for somebody
else's business, which is the definition of platform authority.

Deleting a customer's workspace is still absent, and now deliberately rather
than for want of an audit trail: ending the relationship is the customer's
decision and lives on their own router. Staff who genuinely must force one are
doing something that deserves a runbook rather than a button.

**The reads on this router are audited, and no other read in the API is.** Every
platform *write* was already recorded and no platform read was, which was
defensible while the reads were aggregates and stops being defensible the moment
a customer asks who looked at their workspace. The entries name the actor, the
class of data reached and the workspace when one was named - never a search
term, a workspace name or anything a customer wrote (ADR-095).

What is absent is as considered as what is here. There are no revenue figures,
because revenue is a question about subscriptions and there are none until Phase
13. A plausible zero on a dashboard is worse than an absent field.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import (
    AccountServiceDep,
    PlatformAccessAuditDep,
    PlatformAccountTargetDep,
    PlatformAnalyticsServiceDep,
    PlatformAuditLogRepositoryDep,
    PlatformInvoiceServiceDep,
    PlatformStaffDep,
    WorkspaceServiceDep,
)
from app.api.route import CommittingRoute
from app.db.models.audit import AuditAction
from app.db.models.enums import TenantStatus
from app.platform.platform_analytics import DEFAULT_PAGE, MAX_PAGE
from app.schemas.audit import AuditEntryRead
from app.schemas.auth import AccountStateResponse
from app.schemas.invoice import (
    InvoiceRead,
    InvoiceVoidRequest,
    PaymentRead,
    PaymentRecordRequest,
)
from app.schemas.platform import PlatformOverviewRead, WorkspacePageRead
from app.schemas.workspace import OwnershipRepairRequest, WorkspaceSuspendRequest
from app.schemas.workspace import WorkspaceRead as WorkspaceStateRead

router = APIRouter(route_class=CommittingRoute, prefix="/platform", tags=["platform"])

SinceQuery = Annotated[datetime | None, Query(description="Start of the window, inclusive (UTC)")]
UntilQuery = Annotated[datetime | None, Query(description="End of the window, exclusive (UTC)")]
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=200)]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
OffsetQuery = Annotated[int, Query(ge=0)]


@router.get("/overview", response_model=PlatformOverviewRead)
async def platform_overview(
    staff: PlatformStaffDep,
    analytics: PlatformAnalyticsServiceDep,
    access: PlatformAccessAuditDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
) -> PlatformOverviewRead:
    """Workspaces, connected numbers and platform-wide consumption for a window."""
    overview = await analytics.overview(since=since, until=until)
    access.overview_read(actor=staff.user, windowed=since is not None or until is not None)
    return PlatformOverviewRead.from_overview(overview)


@router.get("/tenants", response_model=WorkspacePageRead)
async def platform_tenants(
    staff: PlatformStaffDep,
    analytics: PlatformAnalyticsServiceDep,
    access: PlatformAccessAuditDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
    search: SearchQuery = None,
    status: TenantStatus | None = None,
    limit: LimitQuery = DEFAULT_PAGE,
    offset: OffsetQuery = 0,
) -> WorkspacePageRead:
    """Workspaces and what each consumed, searchable by name or address.

    Offset paging rather than the cursors the tenant API uses: this list is
    sorted by name and searched by hand, and an operator wants page three of
    forty results rather than a stable feed. `total` is the number matching the
    filter, so paging does not have to guess where the results end.
    """
    page = await analytics.workspaces(
        since=since,
        until=until,
        search=search,
        status=status,
        limit=limit,
        offset=offset,
    )
    # After the read, and recorded whatever it found. An empty page is still an
    # operator having looked, and the trail answers "who looked" rather than
    # "who found something".
    access.workspaces_read(
        actor=staff.user,
        returned=len(page.rows),
        # The term itself is never recorded: somebody searching for one business
        # types an address as readily as a name (ADR-095).
        searched=search is not None,
        filtered=status is not None,
    )
    return WorkspacePageRead.from_page(page)


@router.post("/invoices/{invoice_id}/payments", response_model=PaymentRead)
async def record_payment(
    invoice_id: uuid.UUID,
    payload: PaymentRecordRequest,
    staff: PlatformStaffDep,
    invoices: PlatformInvoiceServiceDep,
) -> PaymentRead:
    """Record money that arrived outside the system. Platform staff only.

    A bank transfer, a card taken over the phone. It is here rather than on the
    workspace's own invoice routes because the act is an assertion that somebody
    has seen the money - and a customer able to make that assertion about their
    own invoice pays nothing.

    Writing, not reading: this one is an exception to the read-only rule the
    rest of this module keeps, and it is the narrowest possible one. It cannot
    change what an invoice says, only record a payment against it.
    """
    payment = await invoices.record_payment(
        invoice_id=invoice_id,
        amount=payload.amount,
        provider=payload.provider,
        reference=payload.reference,
        actor=staff.user,
    )
    return PaymentRead.from_model(payment)


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceRead)
async def void_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceVoidRequest,
    staff: PlatformStaffDep,
    invoices: PlatformInvoiceServiceDep,
) -> InvoiceRead:
    """Withdraw an invoice that should not have been issued. Platform staff only.

    Voided rather than deleted or edited: the customer has seen it. A paid
    invoice cannot be voided - that is a refund, and a different conversation.
    """
    voided = await invoices.void(invoice_id, reason=payload.reason, actor=staff.user)
    return InvoiceRead.from_model(voided)


@router.get("/audit-logs", response_model=list[AuditEntryRead])
async def platform_audit_logs(
    staff: PlatformStaffDep,
    entries: PlatformAuditLogRepositoryDep,
    access: PlatformAccessAuditDep,
    tenant_id: uuid.UUID | None = None,
    action: Annotated[list[AuditAction] | None, Query()] = None,
    actor_id: uuid.UUID | None = None,
    since: SinceQuery = None,
    until: UntilQuery = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEntryRead]:
    """Every recorded act, across every workspace and the platform itself.

    `tenant_id` narrows; it does not scope. Omitting it returns platform
    actions alongside workspace ones, which is the view an investigation needs -
    and the entries it shows include the ones generated by the people reading
    it, because the platform owner is not exempt from the trail.
    """
    rows = await entries.list_entries(
        tenant_id=tenant_id,
        actions=action,
        actor_id=actor_id,
        since=since,
        until=until,
        limit=limit,
    )
    # The deepest read on this surface, and the one whose own entry an
    # investigation is most likely to want: reading a workspace's trail is
    # reading everything its people have done.
    access.audit_log_read(actor=staff.user, tenant_id=tenant_id, returned=len(rows))
    return [AuditEntryRead.from_model(row) for row in rows]


@router.post(
    "/users/{user_id}/disable",
    response_model=AccountStateResponse,
    summary="Suspend an account and end every session it holds",
)
async def disable_user(
    user_id: uuid.UUID,
    staff: PlatformAccountTargetDep,
    accounts: AccountServiceDep,
) -> AccountStateResponse:
    """Platform-authorized, and deliberately not available to a workspace.

    An account is a **global identity**: one person reaches every workspace they
    belong to through it. A tenant administrator able to disable one could evict
    somebody from workspaces that administrator has nothing to do with - which
    is why removing a person from *one* workspace is a different operation
    against a different object, and is still missing (see docs/SECURITY.md).

    Ends every session immediately rather than at token expiry, because
    `users.token_version` is checked on the row that authentication already
    loads (ADR-036).
    """
    user = await accounts.disable(user_id=user_id, actor=staff.user)
    return AccountStateResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        token_version=user.token_version,
    )


@router.post(
    "/users/{user_id}/enable",
    response_model=AccountStateResponse,
    summary="Restore an account without restoring its old sessions",
)
async def enable_user(
    user_id: uuid.UUID,
    staff: PlatformStaffDep,
    accounts: AccountServiceDep,
) -> AccountStateResponse:
    """Re-enabling bumps the version too, and that is the point.

    The one account route that keeps plain `PlatformStaffDep`, deliberately.
    Its two neighbours became target-aware because they *remove* authority, and
    restoring an account cannot: enabling a suspended platform owner puts an
    owner back, which is the direction the AUTHZ-01 guards exist to protect. It
    is also the recovery path - an admin who finds the installation's owners
    suspended must be able to undo that without one of them to authorise it.


    A token minted before the suspension may still be signed and unexpired.
    Without the bump, restoring the account would hand that token its authority
    back - so a disable/enable cycle would resurrect exactly the credentials the
    disable existed to kill. Somebody returning from suspension signs in again.
    """
    user = await accounts.enable(user_id=user_id, actor=staff.user)
    return AccountStateResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        token_version=user.token_version,
    )


@router.delete(
    "/users/{user_id}",
    response_model=AccountStateResponse,
    summary="Permanently tombstone an account and end every session",
)
async def delete_user(
    user_id: uuid.UUID,
    staff: PlatformAccountTargetDep,
    accounts: AccountServiceDep,
) -> AccountStateResponse:
    """Platform lifecycle operation; deleted identities are never reusable."""
    user = await accounts.delete(user_id=user_id, actor=staff.user)
    return AccountStateResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        token_version=user.token_version,
    )


@router.post(
    "/tenants/{tenant_id}/suspend",
    response_model=WorkspaceStateRead,
    summary="Stop serving a workspace",
    responses={
        404: {"description": "No such workspace."},
        409: {"description": "Already suspended, or deleted."},
    },
)
async def suspend_workspace(
    tenant_id: uuid.UUID,
    payload: WorkspaceSuspendRequest,
    staff: PlatformStaffDep,
    workspaces: WorkspaceServiceDep,
) -> WorkspaceStateRead:
    """Platform authority over a customer's workspace, and the state it reaches.

    `TenantStatus.SUSPENDED` has existed since the first tenancy migration and
    nothing has ever written it, so the docstring at the top of this module -
    "suspending or deleting a workspace remains absent" - described a real gap.
    This closes half of it. The other half, deleting a customer's workspace,
    stays absent on purpose: ending the relationship is the customer's decision
    and lives on their own router, and staff who needed to force one would be
    doing something deliberate enough to deserve a runbook rather than a button.

    **What suspension does.** Every workspace-scoped route refuses on the next
    request, because `get_active_workspace` reads `Tenant.is_active` and a
    suspended tenant is not active. Nothing else changes: memberships,
    subscription, data and number claims are all left exactly as they are, so
    `restore` returns the workspace to precisely the state it was in.

    **What it does not do is touch anybody's account.** No session is revoked,
    and that is deliberate rather than an omission - a person in this workspace
    and two others would otherwise be signed out of all three, which punishes
    the wrong people for a decision about one workspace. Their account keeps
    working; this workspace stops.

    Billing is deliberately untouched too, and this is the decision most worth
    being explicit about: a suspended workspace keeps its subscription and its
    period keeps running. Suspension here is an operational stop - an abuse
    investigation, a legal hold - not a statement about money, and the billing
    sweep has its own separate `SUBSCRIPTION_SUSPENDED` state for an unpaid
    invoice (ADR-061). An operator who means to stop charging cancels the
    subscription as well; the two are not the same act and were never intended
    to be. See docs/BILLING.md.
    """
    tenant = await workspaces.suspend(
        tenant_id=tenant_id,
        actor=staff.user,
        reason=payload.reason,
    )
    return WorkspaceStateRead(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        is_active=tenant.is_active,
    )


@router.post(
    "/tenants/{tenant_id}/restore",
    response_model=WorkspaceStateRead,
    summary="Return a suspended workspace to service",
    responses={
        404: {"description": "No such workspace."},
        409: {"description": "Not suspended, or deleted."},
    },
)
async def restore_workspace(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    workspaces: WorkspaceServiceDep,
) -> WorkspaceStateRead:
    """The exact inverse of suspension, and nothing more.

    It resurrects nothing. No session is reinstated, because none was revoked;
    no membership is restored, because none was withdrawn. People who could use
    the workspace before can use it again on their next request, and anybody who
    lost access for a *different* reason - removed by an administrator, disabled
    at the platform - stays without it. That is the property worth checking
    after any restore: a lifecycle operation that quietly gave access back to
    somebody a customer had removed would be a security defect wearing the shape
    of a convenience.

    A **deleted** workspace is refused here. Undoing a customer's decision to
    close their business is not something staff should be able to do with one
    request; it is a database operation with the deliberation that implies
    (docs/RUNBOOK.md).
    """
    tenant = await workspaces.restore(tenant_id=tenant_id, actor=staff.user)
    return WorkspaceStateRead(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        is_active=tenant.is_active,
    )


@router.post(
    "/tenants/{tenant_id}/ownership",
    response_model=WorkspaceStateRead,
    summary="Give an ownerless workspace an owner again",
    responses={
        404: {"description": "No such workspace."},
        409: {
            "description": (
                "The workspace already has an owner, is deleted, "
                "or that person cannot take ownership."
            )
        },
    },
)
async def repair_workspace_ownership(
    tenant_id: uuid.UUID,
    payload: OwnershipRepairRequest,
    staff: PlatformStaffDep,
    workspaces: WorkspaceServiceDep,
) -> WorkspaceStateRead:
    """The recovery half of platform account deletion.

    Deleting a user is allowed to remove a workspace's last owner - an abusive
    or compromised account must not become undeletable by owning something - and
    the workspace is suspended when that happens, because ACTIVE with zero
    owners is a state nobody can administer: inviting an owner, changing the
    plan and closing the workspace are all owner-only. This is how it gets back.

    **Deliberately the smallest power that achieves it.** It promotes somebody
    who is *already* a member; it cannot add an account to a customer's
    workspace, staff's own included, which is the escalation this endpoint would
    otherwise be. It refuses a workspace that still has an owner, so it is not a
    general role-editing tool. A revoked membership is eligible, because the
    colleagues left behind are frequently the ones removed alongside the owner.

    The workspace stays **suspended**. Restoring it is a separate, deliberate
    call by somebody who can see that ownership is sound, and `restore` re-reads
    the owner set under the same lock - so the two cannot interleave into the
    restoration of something still ownerless.
    """
    membership = await workspaces.repair_ownership(
        tenant_id=tenant_id,
        actor=staff.user,
        user_id=payload.user_id,
    )
    tenant = await workspaces.get(tenant_id=membership.tenant_id)
    return WorkspaceStateRead(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        is_active=tenant.is_active,
    )
