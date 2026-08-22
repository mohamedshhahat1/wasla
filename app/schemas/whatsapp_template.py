"""Template registry API contracts.

There is no create or update request here, and that is the contract. A template
is drafted and approved in the WhatsApp Business Manager; anything this API
accepted would be a local fiction that Meta would reject at send time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.services.template_service import SyncOutcome


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    meta_template_id: str | None
    name: str
    language: str
    category: TemplateCategory
    status: TemplateStatus
    body_text: str | None
    components: list[dict[str, Any]] | None
    variable_count: int
    quality_rating: str | None
    rejection_reason: str | None
    synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, template: WhatsAppTemplate) -> Self:
        return cls.model_validate(template)


class TemplateListResponse(BaseModel):
    templates: list[TemplateRead]


class TemplateSyncResponse(BaseModel):
    """What a sync changed, so a workspace can see it did something."""

    account_id: uuid.UUID
    created: int
    updated: int
    withdrawn: int

    @classmethod
    def from_outcome(cls, outcome: SyncOutcome) -> Self:
        return cls(
            account_id=outcome.account_id,
            created=outcome.created,
            updated=outcome.updated,
            withdrawn=outcome.withdrawn,
        )


__all__ = ["TemplateListResponse", "TemplateRead", "TemplateSyncResponse"]
