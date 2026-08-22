"""Billing endpoints.

The catalogue is readable by any member — it is a pricing page, and there is
nothing in it a colleague should be kept from. Everything that changes what the
workspace pays takes an owner: choosing a plan, changing it and cancelling are
the three actions with a bill attached, and an administrator who can invite
colleagues is not thereby someone who can commit the company to a subscription.

Every state change is a named route rather than a `PATCH` on a status field. A
route that accepts `{"status": "active"}` is a route that lets a customer end
their own trial and start a free forever, and no validation afterwards makes
that a good API.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    EntitlementServiceDep,
    PlanRepositoryDep,
    SubscriptionServiceDep,
    TenantOwnerDep,
)
from app.schemas.billing import (
    CancellationRequest,
    EntitlementRead,
    PlanRead,
    PlanSelectionRequest,
    SubscriptionRead,
    SubscriptionStateRead,
)
from app.services.entitlement_service import EntitlementService
from app.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/billing", tags=["billing"])


async def _state(
    subscriptions: SubscriptionService,
    entitlements: EntitlementService,
) -> SubscriptionStateRead:
    """The subscription and the limits it implies, in one shape.

    Both halves are read even when there is no subscription: the entitlements
    then come from the default plan, which is what that workspace is actually
    being held to, and returning an empty list would say the opposite.
    """
    subscription = await subscriptions.get()
    read = None
    if subscription is not None:
        plan = await subscriptions.plan_for(subscription)
        read = SubscriptionRead.from_model(subscription, plan=plan)

    snapshot = await entitlements.snapshot()
    return SubscriptionStateRead(
        subscription=read,
        entitlements=[EntitlementRead.from_entitlement(item) for item in snapshot],
    )


@router.get("/plans", response_model=list[PlanRead])
async def list_plans(
    workspace: ActiveWorkspaceDep,
    plans: PlanRepositoryDep,
) -> list[PlanRead]:
    """The catalogue a workspace may choose from.

    Bespoke plans and retired ones are excluded: one is not on offer to anybody
    but the customer it was written for, and the other is kept only so existing
    subscriptions keep meaning what they meant.
    """
    return [PlanRead.from_model(plan) for plan in await plans.list_plans()]


@router.get("/subscription", response_model=SubscriptionStateRead)
async def read_subscription(
    workspace: ActiveWorkspaceDep,
    subscriptions: SubscriptionServiceDep,
    entitlements: EntitlementServiceDep,
) -> SubscriptionStateRead:
    """This workspace's subscription and where it stands against its limits."""
    return await _state(subscriptions, entitlements)


@router.post(
    "/subscription",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_subscription(
    payload: PlanSelectionRequest,
    workspace: TenantOwnerDep,
    subscriptions: SubscriptionServiceDep,
) -> SubscriptionRead:
    """Choose a plan for a workspace that has none. Owners only.

    The trial, if there is one, comes from the plan. A caller that could ask for
    a trial length is a caller that can ask for a thousand days.
    """
    subscription = await subscriptions.start(
        plan_code=payload.plan_code,
        actor=workspace.user,
    )
    plan = await subscriptions.plan_for(subscription)
    return SubscriptionRead.from_model(subscription, plan=plan)


@router.post("/subscription/plan", response_model=SubscriptionRead)
async def change_plan(
    payload: PlanSelectionRequest,
    workspace: TenantOwnerDep,
    subscriptions: SubscriptionServiceDep,
) -> SubscriptionRead:
    """Upgrade or downgrade, effective now. Owners only.

    The billing period restarts, which cuts both ways: the new plan's allowances
    start now, and so does its period. No proration — money is not moved by this
    system yet, and inventing a credit no invoice reflects would be worse than
    not having one.
    """
    subscription = await subscriptions.change_plan(
        plan_code=payload.plan_code,
        actor=workspace.user,
    )
    plan = await subscriptions.plan_for(subscription)
    return SubscriptionRead.from_model(subscription, plan=plan)


@router.post("/subscription/cancel", response_model=SubscriptionRead)
async def cancel_subscription(
    payload: CancellationRequest,
    workspace: TenantOwnerDep,
    subscriptions: SubscriptionServiceDep,
) -> SubscriptionRead:
    """End the subscription. Owners only.

    At the end of the paid period by default; `immediately` gives it up at once,
    including the rest of the period already paid for.
    """
    subscription = await subscriptions.cancel(
        immediately=payload.immediately,
        actor=workspace.user,
    )
    plan = await subscriptions.plan_for(subscription)
    return SubscriptionRead.from_model(subscription, plan=plan)


@router.post("/subscription/resume", response_model=SubscriptionRead)
async def resume_subscription(
    workspace: TenantOwnerDep,
    subscriptions: SubscriptionServiceDep,
) -> SubscriptionRead:
    """Undo a cancellation that has not taken effect yet. Owners only."""
    subscription = await subscriptions.resume(actor=workspace.user)
    plan = await subscriptions.plan_for(subscription)
    return SubscriptionRead.from_model(subscription, plan=plan)


@router.get("/entitlements", response_model=list[EntitlementRead])
async def read_entitlements(
    workspace: ActiveWorkspaceDep,
    entitlements: EntitlementServiceDep,
) -> list[EntitlementRead]:
    """Every limit and how much of it is used.

    Open to any member, unlike usage: "you have three agents left" is something
    the person about to create the fourth one needs to see, whoever pays.
    """
    snapshot = await entitlements.snapshot()
    return [EntitlementRead.from_entitlement(item) for item in snapshot]
