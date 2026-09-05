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

from app.api.route import CommittingRoute
from app.core.config import Settings
from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.exceptions import DependencyUnavailableError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.telemetry import CallOutcome, Provider, record_provider_call
from app.integrations.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentQueue

logger = get_logger(__name__)

SUBSCRIBE_MODE = "subscribe"

# What an accepted delivery from Meta is counted under.
INBOUND = "inbound_webhook"

router = APIRouter(route_class=CommittingRoute, prefix="/webhooks/whatsapp", tags=["WhatsApp"])


# This provider lives here rather than in app/api/dependencies.py because that
# module is the authentication wiring, and this endpoint is unauthenticated by
# necessity: Meta cannot hold a credential of ours.
def get_ingestion_service(session: SessionDep, redis: RedisDep) -> WhatsAppIngestionService:
    return WhatsAppIngestionService(
        session=session,
        queue=AgentQueue(redis.client),
        media_queue=MediaQueue(redis.client),
    )


IngestionServiceDep = Annotated[WhatsAppIngestionService, Depends(get_ingestion_service)]


def _require_signature(*, body: bytes, header: str | None, settings: Settings) -> None:
    """Reject anything not signed with the app secret.

    With no secret configured the behaviour splits on purpose, and **the split
    is by whether anybody outside this machine can reach the endpoint** rather
    than by whether the environment is called production. Exercising the
    inbound flow on a laptop with no Meta account is what the fail-open branch
    is for; `staging` was never that, because Meta has to deliver webhooks to
    it and therefore so can anyone else.

    What that cost while the check read `is_production`: the payload names
    `phone_number_id`, so an unauthenticated caller chose which workspace to
    write into - contacts created, messages injected, agent jobs enqueued - and
    the injected text is read by the agent as customer input, which makes it a
    prompt-injection channel with outbound sends as the effect.

    A public deployment with no secret answers 503 rather than 403, and rather
    than dropping the delivery: it cannot authenticate anything, so it must
    refuse everything, and the refusal has to be the kind Meta retries. That is
    the same answer the Paymob and Resend callbacks already give.

    Production additionally refuses to *start* without the secret
    (`_validate_hardening`), which is stronger and is why this branch was only
    ever reachable on staging. Both guards stay: the configuration gate is
    production-only on purpose, so the runtime one may not lean on it.
    """
    app_secret = settings.meta_app_secret
    if not app_secret:
        if not settings.is_developer_environment:
            logger.error(
                "whatsapp.webhook_unconfigured",
                extra={
                    "event": "whatsapp.webhook_unconfigured",
                    "environment": settings.environment,
                },
            )
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
    # Counted as one inbound delivery, not one per message inside it: what an
    # operator alerts on is Meta having stopped calling, and a delivery
    # carrying three messages is still one call. The messages themselves are
    # already metered per message into `usage_events`.
    await record_provider_call(
        provider=Provider.WHATSAPP, operation=INBOUND, outcome=CallOutcome.SUCCESS
    )
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
