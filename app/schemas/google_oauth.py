"""Request and response bodies for Google sign-in."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.oauth_flow import MAX_STATE_LENGTH
from app.db.models.identity import IdentityProvider
from app.integrations.google.client import MAX_CODE_LENGTH

# Imported rather than redeclared so `extra="forbid"` cannot drift between the
# authentication payloads and these. Private by name, deliberately shared in
# fact: a second copy of the rule is a second thing to forget to change.
from app.schemas.auth import _Payload


class GoogleAuthorizationResponse(BaseModel):
    """Where to send the browser, and how long the attempt stays valid.

    Carries no state value of its own. The state is inside `authorization_url`
    because that is the only place it is needed, and a client that cannot read
    it is a client that cannot accidentally store or forward it.
    """

    authorization_url: str
    expires_in: int


class GoogleCallbackRequest(_Payload):
    """What the frontend hands back after Google redirects to it.

    Both fields are bounded here as well as in the layers below. Validation at
    the edge means an oversized code is refused before it reaches the code that
    would otherwise relay it to Google.
    """

    code: str = Field(min_length=1, max_length=MAX_CODE_LENGTH)
    state: str = Field(min_length=1, max_length=MAX_STATE_LENGTH)


class GoogleIdentityResponse(BaseModel):
    """A connected Google account, as its owner may see it.

    Deliberately without `provider_subject`. It is a stable identifier that
    correlates this person across every service using the same Google account,
    it has no purpose in a client, and the row it would duplicate is already in
    the database for anybody entitled to read it.
    """

    provider: IdentityProvider
    connected_at: datetime
    last_login_at: datetime | None
