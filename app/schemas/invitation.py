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
    """An invitation as an administrator sees it, at every point in its life.

    The token is absent by design, and this is now the *only* representation -
    there used to be a second one that carried the raw token back to whoever
    issued the invitation, because at the time there was no way to deliver it.
    Its own docstring said the field would go once mail delivery existed
    (ADR-057). It does: `InvitationService.issue` queues the token to the
    invited address through the outbox, which is the one destination that
    proves the recipient owns the mailbox.

    Returning it here proved nothing and travelled everywhere - a 201 body
    reaches reverse-proxy logs, APM payloads and browser captures - so the
    credential that joins a workspace was readable by anything that could read
    a response. It is stored only as a hash, so this class cannot reconstruct
    it even if a later field wanted to.
    """

    id: uuid.UUID
    email: str
    role: TenantRole
    status: InvitationStatus
    expires_at: datetime
    accepted_at: datetime | None = None


class InvitationAcceptedResponse(BaseModel):
    """The membership is ready. Signing in is a separate, deliberate step."""

    email: str
    workspace: WorkspaceSummary
