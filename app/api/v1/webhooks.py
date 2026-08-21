"""Meta WhatsApp webhook.

This is the only endpoint reachable without credentials, so it trusts nothing
but the HMAC over the raw request body.

It answers 200 for everything it cannot act on. Meta retries non-2xx responses
and eventually disables a persistently failing subscription, so an error status
for a payload that will never become valid would turn one bad message into an
outage. Only an invalid signature answers 403, because that request was not
Meta's.

No inference happens here. The endpoint stores what arrived and puts a job on the
queue; the worker calls the model.
"""

from __future__ import annotations

import hmac
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.core.config import Settings
from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.exceptions import DependencyUnavailableError, PermissionDeniedError
from app.core.logging import get_logger
from app.integrations.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.queue import AgentQueue

logger = get_logger(__name__)

SUBSCRIBE_MODE = "subscribe"

router = APIRouter(prefix="/webhooks/whatsapp", tags=["WhatsApp"])


# This provider lives here rather than in app/api/dependencies.py because that
# module is the authentication wiring, and this endpoint is unauthenticated by
# necessity: Meta cannot hold a credential of ours.
def get_ingestion_service(session: SessionDep, redis: RedisDep) -> WhatsAppIngestionService:
    return WhatsAppIngestionService(session=session, queue=AgentQueue(redis.client))


IngestionServiceDep = Annotated[WhatsAppIngestionService, Depends(get_ingestion_service)]


def _require_signature(*, body: bytes, header: str | None, settings: Settings) -> None:
    """Reject anything not signed with the app secret.

    With no secret configured the behaviour splits on purpose: production
    refuses to serve the endpoint rather than accept unverified traffic, while
    local and test environments continue with a warning so the flow can be
    exercised without Meta credentials.
    """
    app_secret = settings.meta_app_secret
    if not app_secret:
        if settings.is_production:
            raise DependencyUnavailableError("WhatsApp webhooks are not configured.")
        logger.warning("whatsapp.signature_verification_skipped")
        return

    if not verify_signature(payload=body, header=header, app_secret=app_secret):
        logger.warning("whatsapp.invalid_signature")
        raise PermissionDeniedError("Invalid webhook signature.")


@router.get(
    "",
    response_class=PlainTextResponse,
    summary="Verify the webhook subscription",
)
async def verify_subscription(
    settings: SettingsDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    """Echo Meta's challenge, but only to a caller that knows the token."""
    expected = settings.meta_verify_token
    if not expected:
        raise DependencyUnavailableError("WhatsApp webhook verification is not configured.")

    # Constant-time, and the challenge is never echoed to a failed attempt.
    matches = token is not None and hmac.compare_digest(token, expected)
    if mode != SUBSCRIBE_MODE or not matches or challenge is None:
        logger.warning("whatsapp.verification_rejected", extra={"mode": mode})
        raise PermissionDeniedError("Webhook verification failed.")

    logger.info("whatsapp.verification_succeeded")
    return PlainTextResponse(challenge)


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Receive inbound messages and delivery statuses",
)
async def receive_events(
    request: Request,
    settings: SettingsDep,
    service: IngestionServiceDep,
) -> dict[str, str]:
    """Store what arrived, ask a worker to answer, and return."""
    body = await request.body()
    _require_signature(
        body=body,
        header=request.headers.get(SIGNATURE_HEADER),
        settings=settings,
    )

    payload = _decode(body)
    if payload is None:
        # Retrying will not make it parse. Acknowledge and move on.
        return {"status": "ignored"}

    outcome = await service.ingest(payload)
    logger.info(
        "whatsapp.webhook_received",
        extra={
            "stored": outcome.stored,
            "duplicates": outcome.duplicates,
            "unknown_accounts": outcome.unknown_accounts,
            "inactive_accounts": outcome.inactive_accounts,
            "ignored": outcome.ignored,
            "queued": outcome.queued,
        },
    )
    return {"status": "accepted"}


def _decode(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("whatsapp.unparseable_payload")
        return None
    if not isinstance(decoded, dict):
        logger.warning("whatsapp.unexpected_payload_shape")
        return None
    return decoded
