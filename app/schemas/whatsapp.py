"""WhatsApp account API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import WhatsAppAccountStatus


class _Payload(BaseModel):
    """Request bodies reject unknown fields rather than ignoring them.

    A silently dropped field is a bug report waiting to happen: the caller
    believes it was applied.
    """

    model_config = ConfigDict(extra="forbid")


class WhatsAppAccountConnectRequest(_Payload):
    """Identifiers copied from the Meta app dashboard.

    No tenant field: the workspace comes from the access token.
    """

    phone_number_id: str = Field(min_length=1, max_length=64)
    waba_id: str = Field(min_length=1, max_length=64)
    display_phone_number: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=200)


class WhatsAppAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone_number_id: str
    waba_id: str
    display_phone_number: str
    display_name: str | None
    status: WhatsAppAccountStatus
    created_at: datetime


class WhatsAppAccountListResponse(BaseModel):
    accounts: list[WhatsAppAccountResponse]
