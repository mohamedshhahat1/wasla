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
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.agents.disclosure import MAX_DISCLOSURE_LENGTH
from app.core.timezones import MAX_TIMEZONE_LENGTH, format_offset, offset_of
from app.db.models import Tenant
from app.db.models.enums import TenantRole, TenantStatus
from app.schemas.auth import (
    MAXIMUM_NAME_LENGTH,
    MAXIMUM_SLUG_LENGTH,
    MINIMUM_SLUG_LENGTH,
    SLUG_PATTERN,
)
from app.schemas.text import StorableText

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

    name: StorableText = Field(min_length=1, max_length=MAXIMUM_NAME_LENGTH)
    slug: str = Field(
        min_length=MINIMUM_SLUG_LENGTH,
        max_length=MAXIMUM_SLUG_LENGTH,
        pattern=SLUG_PATTERN,
        description="The workspace's permanent address. Lower-cased on save.",
    )
    # Where the workspace is, from `GET /api/v1/timezones` (TZ-01). Required:
    # quiet hours and every local time a campaign shows are computed in it.
    timezone: StorableText = Field(
        min_length=1,
        max_length=MAX_TIMEZONE_LENGTH,
        description="An IANA zone name from GET /api/v1/timezones, e.g. Africa/Cairo.",
    )


class DisclosureWording(_Payload):
    """A workspace's wording of the automation disclosure, by language (OMNI-041).

    A language left out keeps Wasla's wording; `{}` restores Wasla's for both.
    An empty string is refused: the obligation is not the workspace's to blank.
    """

    en: StorableText | None = Field(default=None, min_length=1, max_length=MAX_DISCLOSURE_LENGTH)
    ar: StorableText | None = Field(default=None, min_length=1, max_length=MAX_DISCLOSURE_LENGTH)


class WorkspaceUpdateRequest(_Payload):
    """A change to the workspace's own details.

    Both optional, and at least one required - enforced in the service, which is
    where "nothing to update" is a 422 rather than a silent success. `slug` is
    owner-only; see `WorkspaceService.update` for why the two fields do not
    carry the same authority.
    """

    name: StorableText | None = Field(default=None, min_length=1, max_length=MAXIMUM_NAME_LENGTH)
    slug: str | None = Field(
        default=None,
        min_length=MINIMUM_SLUG_LENGTH,
        max_length=MAXIMUM_SLUG_LENGTH,
        pattern=SLUG_PATTERN,
    )
    # The workspace's own wording of the automation disclosure, by language
    # (OMNI-041). Replaces Wasla's for the languages given; `{}` restores
    # Wasla's. Only the words: an empty one is refused, because the obligation
    # to disclose is not the workspace's to switch off.
    automation_disclosure: DisclosureWording | None = None
    # The workspace's IANA zone (TZ-01). An owner's or an administrator's to
    # change; audited. Moves where quiet hours fall, never a scheduled time.
    timezone: StorableText | None = Field(
        default=None, min_length=1, max_length=MAX_TIMEZONE_LENGTH
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


class OwnershipRepairRequest(_Payload):
    """Who platform staff are putting back in charge of an orphaned workspace.

    An id rather than an address, matching `OwnershipTransferRequest`: staff are
    looking at the workspace's own member list, and resolving an address would
    turn this into a probe for whether a given person has a Wasla account.
    """

    user_id: uuid.UUID


class WorkspaceSuspendRequest(_Payload):
    """Why the workspace is being suspended. Optional, and recorded verbatim."""

    reason: StorableText | None = Field(default=None, max_length=MAXIMUM_SUSPENSION_REASON_LENGTH)


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
    # The workspace's own disclosure wording, where it set one (OMNI-041).
    automation_disclosure: dict[str, str] | None = None
    # Its IANA zone and that zone's offset from UTC now, daylight saving
    # included (TZ-01): `+03:00`.
    timezone: str
    utc_offset: str

    @classmethod
    def from_tenant(cls, tenant: Tenant, *, at: datetime | None = None) -> Self:
        return cls(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            status=tenant.status,
            is_active=tenant.is_active,
            automation_disclosure=tenant.automation_disclosure,
            timezone=tenant.timezone,
            utc_offset=format_offset(offset_of(tenant.timezone, at or datetime.now(UTC))),
        )


#: A wall-clock time to the minute, `HH:MM` on a 24-hour clock; seconds refused.
CLOCK_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class CampaignSettingsRead(BaseModel):
    """A workspace's quiet hours, in its own zone (TZ-03)."""

    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    # The zone the window is read in, and its offset from UTC now.
    timezone: str
    utc_offset: str


class LocalTimeRead(BaseModel):
    """A wall time in the workspace's zone, resolved by the server (TZ-08).

    The page sends `at_local` on, never an offset it computed itself. On the
    autumn change day a wall time in the repeated hour is `ambiguous`: both
    instants are in `alternatives` and `at_local` is the first. In the spring
    gap it is `nonexistent`: `at_local` is the first instant after the gap.
    """

    at_local: datetime
    at_utc: datetime
    timezone: str
    offset: str
    ambiguous: bool
    nonexistent: bool
    alternatives: list[datetime]


class FollowUpStepPayload(_Payload):
    """One reminder: "after N minutes" (the card sends hours x 60)."""

    delay_minutes: int
    # An instruction to the AI for this reminder, never sent as it is.
    guidance: str | None = Field(default=None, max_length=2000)


class FollowUpSettingsUpdateRequest(_Payload):
    """The whole of the follow-up settings: every field, every time (a `PUT`)."""

    enabled: bool
    channel_types: list[str] = Field(max_length=10)
    steps: list[FollowUpStepPayload] = Field(max_length=20)


class FollowUpStepRead(BaseModel):
    delay_minutes: int
    guidance: str | None


class FollowUpSettingsSummary(BaseModel):
    """The card's header line: "مفعّل · خطوة واحدة"."""

    enabled: bool
    steps_count: int


class FollowUpSettingsRead(BaseModel):
    """A workspace's automatic follow-ups (FUS-01): the owner's card.

    `available_channel_types` are the platforms this deployment can run them
    on; the card greys the others out. `version` goes back as `If-Match`.
    """

    enabled: bool
    channel_types: list[str]
    available_channel_types: list[str]
    steps: list[FollowUpStepRead]
    summary: FollowUpSettingsSummary
    version: int
    updated_at: datetime | None
    updated_by: uuid.UUID | None


class CampaignSettingsUpdateRequest(_Payload):
    """The whole of the quiet hours: every field, every time (a `PUT`)."""

    quiet_hours_enabled: bool
    quiet_hours_start: str = Field(
        max_length=5, pattern=CLOCK_PATTERN, description="Local time, HH:MM."
    )
    quiet_hours_end: str = Field(
        max_length=5, pattern=CLOCK_PATTERN, description="Local time, HH:MM."
    )


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
