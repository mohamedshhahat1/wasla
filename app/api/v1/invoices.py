"""Invoice endpoints.

Reading is a workspace's own business and takes an owner: an invoice says what
the company spends, which is not something every colleague staffing an inbox is
entitled to see.

Recording a payment and voiding an invoice are **platform** actions, not
workspace ones, and that is the important line here. A customer marking their
own invoice paid is a customer paying nothing, and a customer voiding their own
bill is the same thing with extra steps. Both are assertions that somebody at
the platform has seen the money or made the decision, so both sit behind the
platform-role dependency.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import (
    InvoiceServiceDep,
    PlatformInvoiceServiceDep,
    PlatformStaffDep,
    TenantOwnerDep,
)
from app.schemas.invoice import (
    InvoiceRead,
    InvoiceVoidRequest,
    PaymentRead,
    PaymentRecordRequest,
)

router = APIRouter(prefix="/invoices", tags=["billing"])

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


@router.post("/{invoice_id}/payments", response_model=PaymentRead)
async def record_payment(
    invoice_id: uuid.UUID,
    payload: PaymentRecordRequest,
    staff: PlatformStaffDep,
    invoices: PlatformInvoiceServiceDep,
) -> PaymentRead:
    """Record money that arrived outside the system. Platform staff only.

    A workspace cannot mark its own invoice paid, which is the whole reason this
    is a platform route: the act is an assertion that somebody has seen the
    money.
    """
    payment = await invoices.record_payment(
        invoice_id=invoice_id,
        amount=payload.amount,
        provider=payload.provider,
        reference=payload.reference,
    )
    return PaymentRead.from_model(payment)


@router.post("/{invoice_id}/void", response_model=InvoiceRead)
async def void_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceVoidRequest,
    staff: PlatformStaffDep,
    invoices: PlatformInvoiceServiceDep,
) -> InvoiceRead:
    """Withdraw an invoice that should not have been issued. Platform staff only.

    Voided rather than deleted or edited: the customer has seen it. A paid
    invoice cannot be voided — that is a refund, and a different conversation.
    """
    return InvoiceRead.from_model(await invoices.void(invoice_id, reason=payload.reason))
