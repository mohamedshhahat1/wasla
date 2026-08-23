"""Invoice endpoints, for the workspace that owes the money.

Reading takes an owner: an invoice says what the company spends, which is not
something every colleague staffing an inbox is entitled to see.

Recording a payment and voiding a bill are **not here**. They are platform
actions and live under `/platform/invoices/*`, because a customer who can mark
their own invoice paid is a customer who pays nothing, and one who can void
their own bill is the same thing with extra steps. Keeping them on a separate
router also keeps this one uniformly workspace-scoped, which is what lets the
per-workspace rate limit be attached to the whole of it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import InvoiceServiceDep, TenantOwnerDep
from app.api.route import CommittingRoute
from app.schemas.invoice import InvoiceRead, PaymentRead

router = APIRouter(route_class=CommittingRoute, prefix="/invoices", tags=["billing"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=list[InvoiceRead])
async def list_invoices(
    workspace: TenantOwnerDep,
    invoices: InvoiceServiceDep,
    limit: LimitQuery = 50,
) -> list[InvoiceRead]:
    """This workspace's invoices, newest first. Owners only."""
    return [
        InvoiceRead.from_model(invoice) for invoice in await invoices.list_invoices(limit=limit)
    ]


@router.get("/{invoice_id}", response_model=InvoiceRead)
async def read_invoice(
    invoice_id: uuid.UUID,
    workspace: TenantOwnerDep,
    invoices: InvoiceServiceDep,
) -> InvoiceRead:
    """One invoice, exactly as it was issued. Owners only.

    Another workspace's invoice answers not-found rather than forbidden, like
    every other resource here.
    """
    return InvoiceRead.from_model(await invoices.get(invoice_id))


@router.get("/{invoice_id}/payments", response_model=list[PaymentRead])
async def list_payments(
    invoice_id: uuid.UUID,
    workspace: TenantOwnerDep,
    invoices: InvoiceServiceDep,
) -> list[PaymentRead]:
    """Every attempt at collecting this invoice, including the failures.

    The failures are shown deliberately: a customer whose card was declined
    twice should be able to see that without asking.
    """
    payments = await invoices.payments_for(invoice_id)
    return [PaymentRead.from_model(payment) for payment in payments]
