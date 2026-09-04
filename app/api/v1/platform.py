"""Platform administration endpoints.

Behind `PlatformStaffDep`, which is a different authority from every other route
in this API: it is a property of the user, not of a membership. Owning a
workspace grants nothing here, and holding a platform role grants nothing
*inside* a workspace - a platform administrator reading these figures still
cannot open a customer's inbox.

Read-only with one narrow exception - recording a payment against an invoice,
which asserts that somebody has seen money arrive and cannot change what the
invoice says. Suspending or deleting a workspace remains absent: those
are destructive, and the product has no answer yet for what happens to a
suspended workspace's in-flight conversations. The audit trail that would make
them safe to add now exists, and is readable here.

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
    PlatformAnalyticsServiceDep,
    PlatformAuditLogRepositoryDep,
    PlatformInvoiceServiceDep,
    PlatformStaffDep,
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
    staff: PlatformStaffDep,
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
