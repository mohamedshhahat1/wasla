"""Request and response shapes for the password reset flow."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PasswordResetRequestPayload(BaseModel):
    """Who to send a reset link to.

    Forbids extras for the same reason `app.schemas.auth._Payload` does, and
    these two were the last request bodies in the package that did not. Nothing
    here ever read an undeclared field - the account comes from the address and
    the token, never from the body - so this changes no behaviour. It changes
    what a misspelling does: a silently dropped field becomes a 422 that says
    which one.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded so an absurd token is refused by validation rather than hashed.
    # The floor is loose on purpose: a malformed token must reach the service
    # and fail there with the same answer as an unknown one.
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=1, max_length=256)


class PasswordResetRequestedResponse(BaseModel):
    detail: str
