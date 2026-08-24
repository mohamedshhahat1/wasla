"""Resend delivery events.

Unauthenticated by necessity - a provider cannot hold a credential of ours -
so the only thing this endpoint trusts is the Svix HMAC over the exact bytes
that arrived. Everything after verification treats the payload as data rather
than instruction; `app/services/email_event_service.py` states exactly what an
event is allowed to change, and it is deliberately very little.

It answers 200 for everything it cannot act on, for the reason the WhatsApp
webhook does: a provider retries a non-2xx and eventually disables the
endpoint, so an error status for a payload that will never become valid turns
one unrecognised event into a delivery-reporting outage. Only an unverified
request is refused.

The body is the same either way. "accepted" is returned whether the event was
applied, named a message id we never issued, or was a type this system drops -
because a response that distinguished them would be an oracle for which
message ids exist, and a webhook reply is not a debugging channel.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, status

from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import DependencyUnavailableError, PermissionDeniedError
from app.core.logging import get_logger
from app.integrations.email.signature import (
    ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_signature,
)
from app.services.email_event_service import EmailEventService

logger = get_logger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/webhooks/email", tags=["Email"])


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Receive provider delivery events",
)
async def receive_email_events(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, str]:
    """Verify the delivery, apply what it says, and say nothing about it."""
    body = await request.body()

    secret = settings.resend_webhook_secret
    if not secret:
        # The documented contract of RESEND_WEBHOOK_SECRET: absent means refuse
        # every delivery rather than trust any. Deliberately with no
        # development bypass, unlike the WhatsApp webhook - nothing here is
        # needed to exercise the send path locally, which the fake provider
        # covers, so an unverified-in-development mode would buy nothing and
        # cost a route that accepts unsigned traffic.
        logger.warning(
            "email.webhook_unconfigured",
            extra={"event": "email.webhook_unconfigured"},
        )
        raise DependencyUnavailableError("Email webhooks are not configured.")

    if not verify_signature(
        payload=body,
        message_id=request.headers.get(ID_HEADER),
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature_header=request.headers.get(SIGNATURE_HEADER),
        secret=secret,
    ):
        logger.warning(
            "email.webhook_invalid_signature",
            extra={"event": "email.webhook_invalid_signature"},
        )
        raise PermissionDeniedError("Invalid webhook signature.")

    payload = _decode(body)
    if payload is not None:
        outcome = await EmailEventService(session).record(payload)
        logger.info(
            "email.webhook_received",
            extra={"event": "email.webhook_received", "outcome": outcome},
        )

    return {"status": "accepted"}


def _decode(body: bytes) -> dict[str, Any] | None:
    """The payload as an object, or None if it will never be one."""
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("email.webhook_unparseable", extra={"event": "email.webhook_unparseable"})
        return None
    if not isinstance(decoded, dict):
        logger.warning(
            "email.webhook_unexpected_shape",
            extra={"event": "email.webhook_unexpected_shape"},
        )
        return None
    return decoded
