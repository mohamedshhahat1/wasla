"""Workspace membership API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import MembershipStatus, TenantRole


class _Payload(BaseModel):
    """Request bodies reject unknown fields rather than ignoring them."""

    model_config = ConfigDict(extra="forbid")


class MemberResponse(BaseModel):
    """One person's standing in this workspace.

    The membership id is included alongside the user id because the audit trail
    records the membership, and somebody reading a log entry needs to be able to
    match it to a row on this screen.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str | None
    role: TenantRole
    status: MembershipStatus
    joined_at: datetime
    revoked_at: datetime | None = None


class MemberListResponse(BaseModel):
    members: list[MemberResponse]


class MemberReinstateRequest(_Payload):
    """Readmit somebody who was removed, at a stated role.

    The role is required rather than defaulting to whatever they held before.
    Somebody removed as an owner and quietly restored as one is the kind of
    thing an administrator should have to type.
    """

    role: TenantRole = Field(description="The role to readmit this person at.")
