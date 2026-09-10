"""Request and response shapes for the workspace lifecycle.

The slug rules are imported from `app.schemas.auth` rather than restated.
Registration and `POST /workspaces` create the same kind of object, and a second
copy of the pattern is a second policy: the day one is tightened, workspaces
created through the other route keep whatever the old rule allowed, and the
difference shows up as a support ticket about an address that works in one place
and not another.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import TenantRole, TenantStatus
from app.schemas.auth import (
    MAXIMUM_NAME_LENGTH,
    MAXIMUM_SLUG_LENGTH,
    MINIMUM_SLUG_LENGTH,
    SLUG_PATTERN,
)

# What an operator may write when suspending a workspace. Bounded because it is
# free text from a person that lands in an audit row, and audit metadata is read
# by colleagues rather than parsed.
MAXIMUM_SUSPENSION_REASON_LENGTH = 500


class _Payload(BaseModel):
    """Refuses unknown fields, like every other request model in this API.

    A payload carrying `role: "tenant_owner"` alongside the fields that are
    actually read should fail loudly rather than be quietly ignored - somebody
    is either probing or working from documentation that has moved.
    """

    model_config = ConfigDict(extra="forbid")


class WorkspaceCreateRequest(_Payload):
    """Everything needed to open a workspace, and nothing else.

    No role, no status, no plan and no owner. The creator is the owner because
    they made it; the plan is the configured default; the status is active.
    Accepting any of those would be accepting a client's opinion about a
    decision the server has already made.
    """

    name: str = Field(min_length=1, max_length=MAXIMUM_NAME_LENGTH)
    slug: str = Field(
        min_length=MINIMUM_SLUG_LENGTH,
        max_length=MAXIMUM_SLUG_LENGTH,
        pattern=SLUG_PATTERN,
        description="The workspace's permanent address. Lower-cased on save.",
    )


class WorkspaceUpdateRequest(_Payload):
    """A change to the workspace's own details.

    Both optional, and at least one required - enforced in the service, which is
    where "nothing to update" is a 422 rather than a silent success. `slug` is
    owner-only; see `WorkspaceService.update` for why the two fields do not
    carry the same authority.
    """

    name: str | None = Field(default=None, min_length=1, max_length=MAXIMUM_NAME_LENGTH)
    slug: str | None = Field(
        default=None,
        min_length=MINIMUM_SLUG_LENGTH,
        max_length=MAXIMUM_SLUG_LENGTH,
        pattern=SLUG_PATTERN,
    )


class OwnershipTransferRequest(_Payload):
    """Who is taking over.

    An id rather than an email address. The caller is looking at the member list
    they just read, and accepting an address would mean resolving one - which
    turns this into a probe for whether a given person has a Wasla account.
    """

    user_id: uuid.UUID


class WorkspaceDeleteRequest(_Payload):
    """Confirmation that this is the workspace the caller means.

    The value must equal the workspace's own address, checked server-side. A
    frontend modal is not a control - this route is reachable with curl - and a
    boolean `confirm: true` is not one either, because anything a client sends
    once it can send again by accident. Typing the address is the cheapest
    action that cannot be performed absent-mindedly.
    """

    confirmation: str = Field(
        min_length=MINIMUM_SLUG_LENGTH,
        max_length=MAXIMUM_SLUG_LENGTH,
        description="The workspace's address, typed exactly.",
    )


class WorkspaceSuspendRequest(_Payload):
    """Why the workspace is being suspended. Optional, and recorded verbatim."""

    reason: str | None = Field(default=None, max_length=MAXIMUM_SUSPENSION_REASON_LENGTH)


class WorkspaceRead(BaseModel):
    """A workspace as its own members and platform staff see it."""

    id: uuid.UUID
    name: str
    slug: str
    status: TenantStatus
    # True only while the workspace may actually be used. Derived rather than
    # left to the client to work out from `status`, because "active and not
    # deleted" is two fields and a client that checks one of them renders a
    # tombstoned workspace as usable.
    is_active: bool


class WorkspaceCreatedResponse(BaseModel):
    """A new workspace, and the caller's standing in it.

    No token. Selecting the workspace is `POST /auth/workspace`, which mints an
    access token carrying the new `tid` after re-checking membership - and
    keeping issuance in one place is what stops a second, subtly different
    token-minting path from existing (ADR-058).
    """

    workspace: WorkspaceRead
    role: TenantRole


class OwnershipTransferResponse(BaseModel):
    """Both memberships after a transfer, so a client can redraw the roster."""

    workspace: WorkspaceRead
    previous_owner_id: uuid.UUID
    previous_owner_role: TenantRole
    new_owner_id: uuid.UUID
    new_owner_role: TenantRole
