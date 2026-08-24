"""Proving control of the address on an account.

Two routes, and what they deliberately do *not* accept is the design: neither
takes a user id and neither takes an email address. `send` mails the address on
the authenticated account, `verify` checks a code against that same account.
There is no request field pointing at anybody else, so one person verifying
another's address is not a rule enforced here - it is a request that cannot be
expressed.

That is also why these routes cannot be used to enumerate addresses. The only
way to ask about one is to hold a session for the account that owns it.

Neither route is a login. Verification sets `users.email_verified_at` and
grants nothing; see docs/EMAIL_VERIFICATION.md for why that separation is
maintained deliberately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import CurrentUserDep
from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.rate_limit import RateLimiter
from app.core.security import VERIFICATION_CODE_DIGITS
from app.services.email_verification_service import EmailVerificationService

router = APIRouter(prefix="/auth/email/verification", tags=["auth"])

# Room for the six digits plus the spaces and hyphens people paste in from a
# mail client, and no more. The bound is here so an oversized body is refused
# by validation rather than by Argon2 - hashing is the expensive step, and it
# should never be reachable with arbitrary input length.
_MAX_SUBMITTED_LENGTH = VERIFICATION_CODE_DIGITS * 4


def get_email_verification_service(
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
) -> EmailVerificationService:
    """Built with a limiter, like the auth service.

    The limiter is applied by the service rather than by a route dependency
    because both policies count by account, and the account is the
    authenticated caller - which a router-level dependency would have to resolve
    authentication to discover, dragging the whole chain onto routes that then
    could not be reached without a workspace.
    """
    return EmailVerificationService(
        session=session,
        settings=settings,
        limiter=RateLimiter(redis),
    )


EmailVerificationServiceDep = Annotated[
    EmailVerificationService,
    Depends(get_email_verification_service),
]


class VerificationSendResponse(BaseModel):
    """Deliberately just a sentence.

    No indication of whether mail was queued, whether the address was already
    verified, or whether the recipient is suppressed. The caller already knows
    which account it is signed in as, so nothing is being withheld from a
    legitimate client that it cannot see elsewhere - but keeping the response
    uniform means no future change to this route can accidentally turn it into a
    probe.
    """

    message: str


class VerificationConfirmRequest(BaseModel):
    """The code, and nothing else.

    `extra="forbid"` so a client that sends `user_id` or `email` alongside it
    gets a 422 rather than having the field quietly ignored. A silently dropped
    parameter is how somebody comes to believe they can verify another account.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_SUBMITTED_LENGTH,
        description="The six-digit code from the verification email.",
    )


class VerificationConfirmResponse(BaseModel):
    """When the address was proven.

    The timestamp is the whole result. No token is issued and no session
    changes: this route ends with an account property set, not with a
    credential.
    """

    verified_at: datetime


@router.post(
    "/send",
    response_model=VerificationSendResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a verification code to your own email address",
)
async def send_verification_code(
    current_user: CurrentUserDep,
    service: EmailVerificationServiceDep,
) -> VerificationSendResponse:
    """Queue a code to the address on the authenticated account.

    202 rather than 200: the mail is an outbox row when this returns, and the
    worker delivers it. Claiming 200 would assert a send that has not happened -
    the same reason the password reset request answers 202.
    """
    message = await service.request(user=current_user.user)
    return VerificationSendResponse(message=message)


@router.post(
    "/verify",
    response_model=VerificationConfirmResponse,
    status_code=status.HTTP_200_OK,
    summary="Prove control of your own email address",
)
async def verify_email_address(
    payload: VerificationConfirmRequest,
    current_user: CurrentUserDep,
    service: EmailVerificationServiceDep,
) -> VerificationConfirmResponse:
    """Spend a code against the authenticated account.

    Every rejection - wrong, expired, exhausted, superseded, malformed, never
    issued - leaves as the same 400 with the same message. Which condition
    failed is in the audit trail and never in the response.
    """
    outcome = await service.confirm(user=current_user.user, submitted=payload.code)
    return VerificationConfirmResponse(verified_at=outcome.verified_at)
