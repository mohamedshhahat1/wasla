"""Request and response shapes for the password reset flow."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class PasswordResetRequestPayload(BaseModel):
    email: EmailStr


class PasswordResetConfirmPayload(BaseModel):
    # Bounded so an absurd token is refused by validation rather than hashed.
    # The floor is loose on purpose: a malformed token must reach the service
    # and fail there with the same answer as an unknown one.
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=1, max_length=256)


class PasswordResetRequestedResponse(BaseModel):
    detail: str
