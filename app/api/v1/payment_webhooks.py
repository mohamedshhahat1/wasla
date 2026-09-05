"""Payment provider callbacks: the only thing that settles an invoice.

Unauthenticated by necessity - a payment processor cannot hold a credential of
ours - so the single thing this endpoint trusts is the HMAC over the transaction
it was sent. Everything after verification treats the payload as data rather
than instruction, and `CheckoutService.apply` states exactly what a callback is
allowed to change.

**The customer's browser is not a party to this.** Paymob also redirects the
customer back to a return URL with the transaction in the query string; that
redirect is for showing somebody a success page and is worth nothing as
evidence, because anybody can visit a URL. There is deliberately no endpoint
that reads it and settles anything. This is the authoritative signal, and it
arrives server-to-server.

The response is the same for every outcome that is not a verification failure:
applied, duplicate, unmatched and mismatched all answer 200 `{"status":
"received"}`. Two reasons. A provider retries anything that is not a 2xx and
eventually disables an endpoint that keeps failing, so answering 4xx to a
callback naming an unknown payment turns one stray event into a payment-
reporting outage. And a response that distinguished them would confirm which
payment references exist, to a caller who by then has proven only that they
hold the HMAC secret for *some* payload.

Only an unverified request is refused, and it is refused with 401 rather than
200: that one is not a retry worth accepting.
"""

from __future__ import annotations

import json
import uuid
from typing import Final

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import DependencyUnavailableError, PermissionDeniedError
from app.core.logging import get_logger
from app.db.models.invoice import Payment
from app.integrations.billing import build_checkout_provider
from app.integrations.billing.checkout import (
    CallbackVerificationError,
    CheckoutProvider,
    RecurringProvider,
)
from app.integrations.billing.paymob import callback_type
from app.services.checkout_service import CheckoutService
from app.services.payment_method_service import remember_saved_method

logger = get_logger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/webhooks/paymob", tags=["billing"])

# Paymob supplies the digest as a query parameter on the callback it POSTs, as
# documented in step 4 of the HMAC guide ("compare ... with the hmac value
# received in the callback's query parameters"). A body field of the same name
# is accepted as well, because the published sample body does not carry one and
# a provider that starts sending it there should not become an outage.
SIGNATURE_PARAM: Final = "hmac"

# Paymob marks a saved-card notification with this `type`, as against
# `"TRANSACTION"` for a payment. Documented at
# developers.paymob.com/paymob-docs/developers/pay-with-saved-cards/create-card-token
# (read 2026-08-29).
SAVED_CARD_CALLBACK: Final = "TOKEN"


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Receive a payment provider callback",
)
async def receive_payment_callback(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    hmac: str | None = Query(default=None),
) -> dict[str, str]:
    """Verify the callback, apply what it says, and say nothing about it."""
    provider = build_checkout_provider(settings)
    if provider is None:
        # No provider configured means no callback can be authenticated, and an
        # endpoint that cannot authenticate a payment notification must refuse
        # every one. 503 rather than 200 so the provider retries: a deployment
        # that is briefly misconfigured should not silently drop real payments.
        logger.error(
            "billing.callback_without_provider",
            extra={"event": "billing.callback_without_provider"},
        )
        raise DependencyUnavailableError("No payment provider is configured.")

    body = await request.body()
    signature = hmac or _signature_from_body(body)

    # Paymob sends saved-card notifications to the same endpoint, marked by
    # `type`, and signs them over a different set of fields. Dispatching on the
    # declared type trusts it for nothing: a body claiming to be a card token
    # is still checked against the card-token signature, so lying about the
    # type only changes which way it is refused.
    if callback_type(body) == SAVED_CARD_CALLBACK:
        return await _receive_saved_method(session, provider, body=body, signature=signature)

    try:
        event = provider.verify_callback(payload=body, signature=signature)
    except CallbackVerificationError as error:
        # Neither the digest that was sent nor the one that would have matched
        # is logged. A rejected signature is worth knowing about; writing the
        # expected value down next to it is not.
        logger.warning(
            "billing.callback_rejected",
            extra={"event": "billing.callback_rejected", "reason": str(error)},
        )
        raise PermissionDeniedError("The callback could not be verified.") from error

    tenant_id = await _tenant_for(session, event.reference)
    if tenant_id is None:
        # Verified, but naming nothing we issued. Recorded by the log and
        # answered exactly like a success - see the module docstring.
        logger.warning(
            "billing.callback_unknown_payment",
            extra={
                "event": "billing.callback_unknown_payment",
                "provider_event_id": event.event_id,
            },
        )
        return {"status": "received"}

    service = CheckoutService(
        session,
        tenant_id=tenant_id,
        provider=provider,
        default_plan_code=settings.default_plan_code,
    )
    outcome = await service.apply(event)
    logger.info(
        "billing.callback_processed",
        extra={
            "event": "billing.callback_processed",
            "provider_event_id": event.event_id,
            "outcome": outcome,
        },
    )
    return {"status": "received"}


def _signature_from_body(body: bytes) -> str | None:
    """A digest carried in the body rather than the query string.

    Defensive, and deliberately last: the documented location is the query
    parameter. Returns None for anything unparseable, which then fails
    verification - this must never be a path that produces a *valid* signature
    out of a malformed body.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    value = document.get(SIGNATURE_PARAM)
    return value if isinstance(value, str) else None


async def _tenant_for(session: AsyncSession, reference: str | None) -> uuid.UUID | None:
    """Which workspace a callback belongs to, decided by our own reference.

    This lookup is deliberately **not** tenant-scoped, and it is the one place
    in the application that reads a payment without a tenant filter - a
    callback arrives with no session, so there is no workspace to scope by yet.
    What keeps it safe is that the reference is a payment id *we* generated and
    sent to the provider, and the tenant is read off the row rather than from
    anything in the request. Every subsequent operation goes through a
    tenant-scoped service built from that value, so an event can only ever
    affect the workspace that owns the payment it names.
    """
    if not reference:
        return None
    try:
        payment_id = uuid.UUID(reference)
    except ValueError:
        return None

    result = await session.execute(select(Payment.tenant_id).where(Payment.id == payment_id))
    return result.scalar_one_or_none()


async def _receive_saved_method(
    session: AsyncSession,
    provider: CheckoutProvider,
    *,
    body: bytes,
    signature: str | None,
) -> dict[str, str]:
    """A customer kept their card, and the provider is telling us the token.

    Verified with the provider's card-token scheme rather than the transaction
    one - a different field list, so checking it with the wrong list would not
    be checking it at all.

    The workspace is resolved exactly as a payment is: through the reference
    *we* generated for the checkout that saved the card. A token arriving with
    no such reference is recorded in the log and stored nowhere, because a card
    that cannot be attributed to a workspace is a card nobody may charge.
    """
    recurring = provider if isinstance(provider, RecurringProvider) else None
    if recurring is None:  # pragma: no cover - every real provider implements it
        raise DependencyUnavailableError("This provider cannot receive saved cards.")

    try:
        saved = recurring.verify_token_callback(payload=body, signature=signature)
    except CallbackVerificationError as error:
        logger.warning(
            "billing.card_token_rejected",
            extra={"event": "billing.card_token_rejected", "reason": str(error)},
        )
        raise PermissionDeniedError("The callback could not be verified.") from error

    tenant_id = await _tenant_for_order(session, saved.order_reference)
    if tenant_id is None:
        # Verified, but naming no checkout of ours. Answered like a success for
        # the same reason an unmatched payment is - see the module docstring.
        logger.warning(
            "billing.card_token_unmatched",
            extra={
                "event": "billing.card_token_unmatched",
                "provider_token_id": saved.provider_token_id,
            },
        )
        return {"status": "received"}

    _, created = await remember_saved_method(
        session,
        tenant_id=tenant_id,
        provider=provider.name,
        saved=saved,
    )
    logger.info(
        "billing.card_token_processed",
        extra={
            "event": "billing.card_token_processed",
            "tenant_id": str(tenant_id),
            "created": created,
        },
    )
    return {"status": "received"}


async def _tenant_for_order(session: AsyncSession, order_reference: str | None) -> uuid.UUID | None:
    """Which workspace saved this card, decided by our own intention reference.

    Paymob quotes the order back on the token callback. That order was created
    from an intention whose id we stored on the payment as
    `provider_intent_reference`, so the lookup is against something this system
    wrote down - never against a value the caller invented.

    Deliberately not tenant-scoped, for the same reason `_tenant_for` is not: a
    callback arrives with no session, so there is no workspace to scope by yet.
    The tenant is read off the row that the reference resolves to.
    """
    if not order_reference:
        return None

    result = await session.execute(
        select(Payment.tenant_id)
        .where(Payment.provider_intent_reference == str(order_reference))
        .limit(1)
    )
    return result.scalar_one_or_none()
