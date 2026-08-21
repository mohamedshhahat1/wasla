"""Invitation request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import MAXIMUM_PASSWORD_LENGTH, MINIMUM_PASSWORD_LENGTH
from app.db.models import InvitationStatus, TenantRole
from app.schemas.auth import WorkspaceSummary

MAXIMUM_NAME_LENGTH: Final = 200


class _Payload(BaseModel):
    """Base for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class InvitationCreateRequest(_Payload):
    email: EmailStr
    role: TenantRole = TenantRole.MEMBER


class InvitationAcceptRequest(_Payload):
    token: str = Field(min_length=1)
    # Only needed when the invited address has no account yet.
    password: str | None = Field(
        default=None,
        min_length=MINIMUM_PASSWORD_LENGTH,
        max_length=MAXIMUM_PASSWORD_LENGTH,
    )
    full_name: str | None = Field(default=None, max_length=MAXIMUM_NAME_LENGTH)


class InvitationResponse(BaseModel):
    """An invitation as an administrator sees it.

    The token is absent by design: it is stored only as a hash, so it cannot be
    read back out of the API even by the person who issued it.
    """

    id: uuid.UUID
    email: str
    role: TenantRole
    status: InvitationStatus
    expires_at: datetime
    accepted_at: datetime | None = None


class InvitationCreatedResponse(InvitationResponse):
    """The single moment the raw token is visible.

    It is returned to the administrator who issued the invitation because there
    is no mail delivery yet. Once there is, the token stops crossing this
    boundary and goes only to the invited address.
    """

    token: str


class InvitationAcceptedResponse(BaseModel):
    """The membership is ready. Signing in is a separate, deliberate step."""

    email: str
    workspace: WorkspaceSummary
