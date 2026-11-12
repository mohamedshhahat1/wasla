"""The member-readable gate summary of a workspace (FEB-07, ADR-194).

Every value comes from the function that already decides it for the owner's
view - the open renewals `GET /billing/summary` lists, the subscription the
billing routes read, the capacity view `GET /billing/channel-capacity` answers
with - so a member's banner and the owner's billing page can never disagree.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Tenant
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository
from app.schemas.workspace_gates import ChannelCapacityGates, WorkspaceGates
from app.services import channel_capacity_view


async def read_workspace_gates(
    session: AsyncSession, *, tenant: Tenant, settings: Settings
) -> WorkspaceGates:
    """Status, payment due, subscription status, trial end and capacity flags. No amounts."""
    renewals = await InvoiceRepository(session, tenant_id=tenant.id).open_renewals()
    subscription = await SubscriptionRepository(session, tenant_id=tenant.id).get()
    capacity = await channel_capacity_view.read_channel_capacity(session, tenant.id, settings)
    return WorkspaceGates(
        status=tenant.status,
        payment_required=bool(renewals),
        subscription_status=subscription.status if subscription is not None else None,
        trial_ends_at=subscription.trial_ends_at if subscription is not None else None,
        channel_capacity=ChannelCapacityGates(
            over_limit=capacity.over_limit,
            reduction_pending=capacity.reduction is not None,
        ),
    )
