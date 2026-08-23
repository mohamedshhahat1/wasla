"""Template registry endpoints.

Reading is open to any member: a person composing a campaign or a follow-up
needs to know what they are allowed to send. Syncing is an administrative
action — it calls Meta and rewrites the registry — so it takes an admin.

There is deliberately no route that creates, edits or deletes a template.
Approval belongs to Meta, and an endpoint that wrote one locally would be
inventing permission the platform does not have.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.dependencies import ActiveWorkspaceDep, TemplateServiceDep, TenantAdminDep
from app.api.route import CommittingRoute
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus
from app.schemas.whatsapp_template import (
    TemplateListResponse,
    TemplateRead,
    TemplateSyncResponse,
)

router = APIRouter(route_class=CommittingRoute, prefix="/templates", tags=["templates"])


@router.get("", summary="List approved and pending templates")
async def list_templates(
    workspace: ActiveWorkspaceDep,
    templates: TemplateServiceDep,
    account_id: uuid.UUID | None = None,
    status: TemplateStatus | None = None,
    category: TemplateCategory | None = None,
) -> TemplateListResponse:
    rows = await templates.list_templates(
        account_id=account_id,
        status=status,
        category=category,
    )
    return TemplateListResponse(templates=[TemplateRead.from_model(row) for row in rows])


@router.get("/{template_id}", summary="One template")
async def get_template(
    template_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    templates: TemplateServiceDep,
) -> TemplateRead:
    return TemplateRead.from_model(await templates.get(template_id))


@router.post("/sync", summary="Refresh the registry from Meta")
async def sync_templates(
    account_id: uuid.UUID,
    workspace: TenantAdminDep,
    templates: TemplateServiceDep,
) -> TemplateSyncResponse:
    """Ask Meta what this account's templates are and write down the answer.

    Synchronous on purpose. A workspace approves a template and then wants to
    use it, so an answer it can see beats a job it has to wait for. The call is
    bounded by the client's timeout, page size and page count.
    """
    return TemplateSyncResponse.from_outcome(await templates.sync(account_id))
