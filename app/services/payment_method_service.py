"""Cards a workspace has saved, and which one renewals use.

A saved card arrives the same way a payment does: the customer completes a
checkout, chooses to keep the card, and the provider tells us about it on a
signed callback. Nothing here is created from a request body - a client cannot
post a token, because a token somebody could post is a token somebody could
steal from another workspace and charge.

The only thing a customer may do through the API is choose which of *their*
saved cards renewals should use, and remove one.

**No card data is stored.** What comes back is an opaque provider token, the
last four digits the provider already prints on receipts, and the scheme name.
`PaymentMethod` has nowhere to put a card number, which is a stronger guarantee
than a rule saying not to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.integrations.billing.checkout import SavedPaymentMethod
from app.repositories.payment_method_repository import (
    PaymentMethodRepository,
    PlatformPaymentMethodRepository,
)

logger = get_logger(__name__)


class PaymentMethodService:
    """Saved cards for one workspace."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._methods = PaymentMethodRepository(session, tenant_id=tenant_id)

    async def list_methods(self) -> list[PaymentMethod]:
        return await self._methods.list_active()

    async def default_method(self) -> PaymentMethod | None:
        return await self._methods.default_method()

    async def make_default(self, method_id: uuid.UUID) -> PaymentMethod:
        """Choose which card renewals use.

        Clearing the old default and setting the new one happen in the same
        transaction, which is why the database does not enforce "exactly one":
        a constraint would make this a two-statement dance that can fail
        halfway and leave a workspace with none.
        """
        method = await self._require(method_id)
        if not method.is_chargeable:
            raise ConflictError("This card has been removed and cannot be used.")

        for existing in await self._methods.list_active():
            existing.is_default = existing.id == method.id
        await self._session.flush()

        logger.info(
            "billing.payment_method_default_changed",
            extra={
                "event": "billing.payment_method_default_changed",
                "tenant_id": str(self._tenant_id),
                "payment_method_id": str(method.id),
            },
        )
        return method

    async def revoke(self, method_id: uuid.UUID, *, now: datetime | None = None) -> PaymentMethod:
        """Stop using a card, without erasing what it paid for.

        Revoked rather than deleted: payments point at it, and the record of
        which card collected last month's invoice should survive somebody
        tidying their account. A revoked card is never chosen for a renewal.
        """
        moment = now if now is not None else datetime.now(UTC)
        method = await self._require(method_id)
        if not method.is_chargeable:
            return method

        method.status = PaymentMethodStatus.REVOKED
        method.revoked_at = moment
        method.is_default = False
        await self._session.flush()

        logger.info(
            "billing.payment_method_revoked",
            extra={
                "event": "billing.payment_method_revoked",
                "tenant_id": str(self._tenant_id),
                "payment_method_id": str(method.id),
            },
        )
        return method

    async def _require(self, method_id: uuid.UUID) -> PaymentMethod:
        """One card of this workspace's, or a 404.

        Tenant-scoped through the repository, so another workspace's card id is
        indistinguishable from one that does not exist - which matters more
        here than usual, because the thing being addressed can be charged.
        """
        method = await self._methods.get_by_id(method_id)
        if method is None:
            raise NotFoundError("No such payment method.")
        return method


async def remember_saved_method(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    saved: SavedPaymentMethod,
    now: datetime | None = None,
) -> tuple[PaymentMethod, bool]:
    """Record a card the provider says a customer saved. Returns (card, is_new).

    A module function rather than a method because the caller is the webhook,
    which resolves the workspace from a payment *we* issued and then has no
    further need of a service object.

    Idempotent on the provider's token, and by the constraint rather than by
    the read: a saved-card notification is retried like any other callback, and
    two retries arriving together would both find nothing. The savepoint keeps
    the outer transaction usable when one loses.

    The first card a workspace saves becomes its default. Later ones do not,
    because silently moving renewals onto a card somebody added for a one-off
    payment is a surprise nobody asked for.
    """
    del now  # timestamps are database-managed; kept for a uniform signature
    unscoped = PlatformPaymentMethodRepository(session)
    existing = await unscoped.get_by_token(provider=provider, token=saved.token)
    if existing is not None:
        return existing, False

    methods = PaymentMethodRepository(session, tenant_id=tenant_id)
    is_first = await methods.default_method() is None

    try:
        async with session.begin_nested():
            created = methods.create(
                provider=provider,
                provider_token=saved.token,
                provider_token_id=saved.provider_token_id or None,
                masked_pan=saved.masked_pan,
                brand=saved.brand,
                is_default=is_first,
            )
            await session.flush()
    except IntegrityError:
        # Another delivery of the same notification won. Re-read rather than
        # raise: the caller answers 200 either way, and the card exists.
        found = await unscoped.get_by_token(provider=provider, token=saved.token)
        if found is None:  # pragma: no cover - the row that just blocked us
            raise
        return found, False

    logger.info(
        "billing.payment_method_saved",
        extra={
            "event": "billing.payment_method_saved",
            "tenant_id": str(tenant_id),
            "payment_method_id": str(created.id),
            "brand": saved.brand,
            # Never the token: it is what charges the card.
        },
    )
    return created, True
