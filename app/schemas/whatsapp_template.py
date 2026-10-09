"""Template registry API contracts.

There is no create or update request here, and that is the contract. A template
is drafted and approved in the WhatsApp Business Manager; anything this API
accepted would be a local fiction that Meta would reject at send time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field

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
    # The quick-reply payloads this workspace marked as its marketing opt-out
    # (OMNI-030). Additive.
    opt_out_payloads: list[str] | None = None
    quality_rating: str | None
    rejection_reason: str | None
    synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, template: WhatsAppTemplate) -> Self:
        return cls.model_validate(template)


#: How many opt-out payloads one template may carry. A template has at most ten
#: buttons; the bound is generous and finite.
MAX_OPT_OUT_PAYLOADS = 10
#: Meta's longest documented quick-reply payload, for a payload kept verbatim.
MAX_OPT_OUT_PAYLOAD_LENGTH = 1_000


class TemplateOptOutPayloads(BaseModel):
    """The quick-reply payloads that mean "stop marketing messages" on this template.

    A customer tapping a button carrying one is opted out whatever the button's
    words say (OMNI-030). An empty list removes the marks.
    """

    model_config = ConfigDict(extra="forbid")

    payloads: list[Annotated[str, Field(min_length=1, max_length=MAX_OPT_OUT_PAYLOAD_LENGTH)]] = (
        Field(max_length=MAX_OPT_OUT_PAYLOADS)
    )


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
