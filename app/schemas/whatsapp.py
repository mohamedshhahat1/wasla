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
    """A claim on a number, and the credential that proves it (ADR-037).

    No tenant field: the workspace comes from the access token.

    What is *not* here is as deliberate as what is. The display number and the
    verified name used to be typed in and stored; they now come back from Meta
    during verification, so there is nothing for a caller to get wrong and
    nothing to spoof. `waba_id` survives only as an assertion to check - supply
    it and a mismatch is refused, omit it and Meta's answer is used.
    """

    phone_number_id: str = Field(min_length=1, max_length=64)
    # Required. The claim is proven by reading the phone number node with this
    # credential, and there is no other proof: a request without one cannot
    # establish that this workspace controls this number, and the platform
    # credential deliberately does not count - it can read every number the
    # platform is connected to.
    access_token: str = Field(min_length=1, max_length=512)
    # Optional, and checked rather than trusted. Meta names the owning business
    # account; supplying a different one fails the claim instead of being
    # quietly corrected.
    waba_id: str | None = Field(default=None, min_length=1, max_length=64)
    # The workspace's own label for the number - "Support", "Sales". Purely
    # cosmetic and purely local, which is why it is still an input.
    display_name: str | None = Field(default=None, max_length=200)


class WhatsAppAccountVerifyRequest(_Payload):
    """Prove control of a number this workspace already holds (ADR-041).

    No `phone_number_id`: the number comes from the row being verified, so this
    cannot be used to move a claim. Only `connect` claims a number.
    """

    access_token: str = Field(min_length=1, max_length=512)


class WhatsAppAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone_number_id: str
    waba_id: str
    display_phone_number: str
    display_name: str | None
    # What Meta calls this business on this number, recorded at verification.
    verified_name: str | None = None
    status: WhatsAppAccountStatus
    created_at: datetime
    # When control of this number was last proven to Meta. Null only on rows
    # claimed before ownership proof existed, which is exactly the set an
    # operator needs to be able to find.
    ownership_verified_at: datetime | None = None
    # The same fact as a boolean, because the security state of a number should
    # be readable without reasoning about a null timestamp (ADR-041).
    ownership_verified: bool = False
    # Whether this number sends with the workspace's own credential. The
    # *existence* of a credential is operationally useful - somebody has to be
    # able to tell whether a number is configured - and is the only thing about
    # it any response ever discloses.
    has_own_credential: bool = False


class WhatsAppAccountListResponse(BaseModel):
    accounts: list[WhatsAppAccountResponse]
