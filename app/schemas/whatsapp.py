"""WhatsApp account API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import WhatsAppAccountStatus
from app.schemas.text import StorableText


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

    phone_number_id: StorableText = Field(min_length=1, max_length=64)
    # Required. The claim is proven by reading the phone number node with this
    # credential, and there is no other proof: a request without one cannot
    # establish that this workspace controls this number, and the platform
    # credential deliberately does not count - it can read every number the
    # platform is connected to.
    access_token: StorableText = Field(min_length=1, max_length=512)
    # Optional, and checked rather than trusted. Meta names the owning business
    # account; supplying a different one fails the claim instead of being
    # quietly corrected.
    waba_id: StorableText | None = Field(default=None, min_length=1, max_length=64)
    # The workspace's own label for the number - "Support", "Sales". Purely
    # cosmetic and purely local, which is why it is still an input.
    display_name: StorableText | None = Field(default=None, max_length=200)


class WhatsAppAccountVerifyRequest(_Payload):
    """Prove control of a number this workspace already holds (ADR-041).

    No `phone_number_id`: the number comes from the row being verified, so this
    cannot be used to move a claim. Only `connect` claims a number.
    """

    access_token: StorableText = Field(min_length=1, max_length=512)


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


class MessagingLimitRead(BaseModel):
    """A number's business-portfolio messaging limit, and how much of it is used (LIM-07).

    Aggregates of the whole portfolio - every number, every workspace on it -
    and nothing that names another workspace. `messaging_limit` null with
    `known` true is unlimited; `known` false means it was never read and no
    campaign on this number is scheduled or sent.
    """

    tier: str | None
    known: bool
    messaging_limit: int | None
    checked_at: datetime | None
    source: str | None
    campaign_share: float
    campaign_budget: int | None
    used_last_24h: int
    campaign_available_now: int | None
    next_capacity_at: datetime | None
    next_capacity_at_local: datetime | None = None
    shared_with_other_numbers: bool
    shared_with_other_workspaces: bool


class WhatsAppAccountListResponse(BaseModel):
    accounts: list[WhatsAppAccountResponse]


# ------------------------------------------------------------- Embedded Signup


class EmbeddedSignupConfigRead(BaseModel):
    """What the browser needs to open Meta's popup (CF-03). Nothing secret.

    `enabled` false means this deployment cannot finish an onboarding, and
    every other field is then null: only the access-token form is offered.
    """

    enabled: bool
    app_id: str | None = None
    config_id: str | None = None
    es_version: str | None = None
    graph_version: str | None = None
    coexistence: bool | None = None


class EmbeddedSignupRequestBody(_Payload):
    """The popup's code and session ids (EX-01). The ids are assertions, proven with Meta.

    `messaging_account_id` is v5's `messaging_account_ids[0]`; `waba_id` is v4's.
    One of the two is required. `is_on_biz_app` is a hint only (CX-02).
    """

    code: StorableText = Field(min_length=1, max_length=2048)
    phone_number_id: StorableText = Field(min_length=1, max_length=64)
    messaging_account_id: StorableText | None = Field(default=None, min_length=1, max_length=64)
    waba_id: StorableText | None = Field(default=None, min_length=1, max_length=64)
    business_id: StorableText | None = Field(default=None, max_length=64)
    is_on_biz_app: bool | None = None
    es_version: Literal["v4", "v5"] | None = None


class OnboardingHistoryRead(BaseModel):
    requested_at: datetime | None
    progress: int | None
    phase_max: int | None
    completed_at: datetime | None
    declined_at: datetime | None


class OnboardingContactsRead(BaseModel):
    requested_at: datetime | None


class ProviderDisconnectRead(BaseModel):
    """Meta's own reason word (EXT-ES-32), who started it, and when (AU-03)."""

    reason: str | None
    initiated_by: str | None
    at: datetime | None


class OnboardingStatusRead(BaseModel):
    """How a number was connected and how far its onboarding got (OS-02).

    Never a token, a PIN, a request id or Meta's payload (OS-03). An assisted
    connection answers `method: assisted` and nulls (OS-04).
    """

    connection_id: uuid.UUID
    method: str
    mode: str | None
    onboarded_at: datetime | None
    subscribed_at: datetime | None
    registered_at: datetime | None
    registration_needed: bool
    sync_deadline_at: datetime | None
    contacts: OnboardingContactsRead
    history: OnboardingHistoryRead
    provider_disconnect: ProviderDisconnectRead | None
    reminder_days: int | None


class EmbeddedSignupResponse(BaseModel):
    """The connection, as the assisted connect returns it, and its onboarding (EX-12)."""

    account: WhatsAppAccountResponse
    onboarding: OnboardingStatusRead
