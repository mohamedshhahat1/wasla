"""Authentication request and response schemas."""

from __future__ import annotations

import uuid
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import MAXIMUM_PASSWORD_LENGTH, MINIMUM_PASSWORD_LENGTH
from app.db.models import PlatformRole, TenantRole

# Letters, digits and internal hyphens. Case is accepted and normalised on the
# way in, so a workspace address is never case-sensitive.
SLUG_PATTERN: Final = r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
MAXIMUM_NAME_LENGTH: Final = 200
MAXIMUM_SLUG_LENGTH: Final = 100


class _Payload(BaseModel):
    """Base for request bodies.

    Unknown fields are rejected: a misspelled field must fail loudly rather
    than be silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


class RegistrationRequest(_Payload):
    email: EmailStr
    password: str = Field(
        min_length=MINIMUM_PASSWORD_LENGTH,
        max_length=MAXIMUM_PASSWORD_LENGTH,
    )
    full_name: str | None = Field(default=None, max_length=MAXIMUM_NAME_LENGTH)
    workspace_name: str = Field(min_length=1, max_length=MAXIMUM_NAME_LENGTH)
    workspace_slug: str = Field(
        min_length=2,
        max_length=MAXIMUM_SLUG_LENGTH,
        pattern=SLUG_PATTERN,
    )


class LoginRequest(_Payload):
    email: EmailStr
    # No minimum here: password policy is enforced when a password is set, and
    # restating it on login would only describe the rules to an attacker.
    password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_LENGTH)
    workspace_slug: str | None = Field(default=None, max_length=MAXIMUM_SLUG_LENGTH)


class RefreshRequest(_Payload):
    refresh_token: str = Field(min_length=1)
    workspace_slug: str | None = Field(default=None, max_length=MAXIMUM_SLUG_LENGTH)


class LogoutRequest(_Payload):
    refresh_token: str = Field(min_length=1)


class WorkspaceSwitchRequest(_Payload):
    workspace_slug: str = Field(min_length=2, max_length=MAXIMUM_SLUG_LENGTH)


class PasswordChangeRequest(_Payload):
    """Changing a password from inside a session.

    The current password is the proof, which is what makes this reachable
    without an email provider - unlike a reset, which has to send a token to an
    address somebody controls. See docs/SECURITY.md.

    `current_password` carries no minimum: restating the policy on the way in
    would describe the rules to somebody guessing, and it is compared against a
    hash rather than a rule. The maximum is enforced on both, because an
    unbounded string reaching Argon2 is a way to spend CPU.
    """

    current_password: str = Field(min_length=1, max_length=MAXIMUM_PASSWORD_LENGTH)
    new_password: str = Field(
        min_length=MINIMUM_PASSWORD_LENGTH,
        max_length=MAXIMUM_PASSWORD_LENGTH,
    )


class AccountStateResponse(BaseModel):
    """What an account lifecycle action left behind.

    Carries the version so an administrator can see that a revocation took
    effect. The number says nothing about any token - it is a counter, not a
    secret - and no token is ever returned here or written to a log.
    """

    id: uuid.UUID
    email: str
    is_active: bool
    token_version: int


class WorkspaceSummary(BaseModel):
    """A workspace as seen by one member, including their role in it."""

    id: uuid.UUID
    name: str
    slug: str
    role: TenantRole


class SessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = Field(default="bearer")
    expires_in: int
    active_workspace: WorkspaceSummary | None = None


class AccessTokenResponse(BaseModel):
    """Result of switching workspace: the refresh token is left untouched."""

    access_token: str
    token_type: Literal["bearer"] = Field(default="bearer")
    expires_in: int
    active_workspace: WorkspaceSummary


class ProfileResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None = None
    platform_role: PlatformRole | None = None
    workspaces: list[WorkspaceSummary] = Field(default_factory=list)
