"""What any member may know about the gates in front of their workspace (FEB-07, ADR-194).

A member cannot read the billing summary - it carries amounts, invoices and the
card - yet the interface has to tell every member that the workspace is
suspended, that a payment is due, that the trial ends on a date or that channel
capacity is over its limit. This is that knowledge and nothing else: flags,
statuses and one date. No amount, price, currency, invoice, card, payment
method, limit or usage number belongs here; a test holds the keys to exactly
this set.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import TenantStatus
from app.db.models.billing import SubscriptionStatus


class ChannelCapacityGates(BaseModel):
    """Channel capacity as two flags, without the numbers behind them."""

    model_config = ConfigDict(title="ChannelCapacityGates")

    over_limit: bool = Field(
        description="More channel connections are active than the plan allows now."
    )
    reduction_pending: bool = Field(
        description="A capacity reduction is open, waiting for the owner's choice or its grace end."
    )


class WorkspaceGates(BaseModel):
    """The banners a member's interface may need to draw for the current workspace."""

    model_config = ConfigDict(title="WorkspaceGates")

    status: TenantStatus = Field(description="The workspace's own status.")
    payment_required: bool = Field(
        description="A renewal invoice is issued and still owed (what Pay Renewal pays)."
    )
    subscription_status: SubscriptionStatus | None = Field(
        description="The subscription's status, or null when the workspace has none."
    )
    trial_ends_at: datetime | None = Field(
        description="When the trial ends, while the subscription has one; null otherwise."
    )
    channel_capacity: ChannelCapacityGates
