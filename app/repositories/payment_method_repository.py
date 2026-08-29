"""Data access for saved cards, scoped like every other workspace-owned table."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement

from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.repositories.base import BaseRepository, TenantScopedRepository


class PaymentMethodRepository(TenantScopedRepository[PaymentMethod]):
    """One workspace's saved cards."""

    model = PaymentMethod

    def _tenant_filter(self) -> ColumnElement[bool]:
        return PaymentMethod.tenant_id == self.tenant_id

    async def get_by_id(self, method_id: uuid.UUID) -> PaymentMethod | None:
        return await self._first(self._select().where(PaymentMethod.id == method_id))

    async def list_active(self) -> list[PaymentMethod]:
        """Cards that may still be charged, newest first.

        Revoked ones are excluded rather than deleted: a payment collected with
        a card is still a payment collected with *that* card, so the row stays
        and stops being offered.
        """
        return await self._all(
            self._select()
            .where(PaymentMethod.status == PaymentMethodStatus.ACTIVE)
            .order_by(PaymentMethod.created_at.desc())
        )

    async def default_method(self) -> PaymentMethod | None:
        """The card a renewal would use, if this workspace has one.

        Both conditions matter. A card marked default but since revoked must
        not be charged, and returning it would mean a renewal attempt against a
        card the customer removed.
        """
        return await self._first(
            self._select()
            .where(PaymentMethod.is_default.is_(True))
            .where(PaymentMethod.status == PaymentMethodStatus.ACTIVE)
        )

    def create(
        self,
        *,
        provider: str,
        provider_token: str,
        provider_token_id: str | None,
        masked_pan: str | None,
        brand: str | None,
        is_default: bool,
    ) -> PaymentMethod:
        return self.add(
            PaymentMethod(
                tenant_id=self.tenant_id,
                provider=provider,
                provider_token=provider_token,
                provider_token_id=provider_token_id,
                masked_pan=masked_pan,
                brand=brand,
                status=PaymentMethodStatus.ACTIVE,
                is_default=is_default,
            )
        )


class PlatformPaymentMethodRepository(BaseRepository[PaymentMethod]):
    """Saved cards across every workspace, for the callback that creates one.

    Deliberately unscoped and deliberately its own class, like the platform
    invoice and subscription readers. A card-token notification arrives with no
    session, so there is no workspace to scope by until the checkout it came
    from has been resolved - and that resolution goes through a payment row we
    wrote ourselves.
    """

    model = PaymentMethod

    async def get_by_token(self, *, provider: str, token: str) -> PaymentMethod | None:
        """An existing card by the provider's token.

        What makes a repeated saved-card notification a no-op rather than a
        second card. The unique constraint is the guarantee; this read is what
        produces the quiet answer instead of an integrity error.
        """
        return await self._first(
            self._select()
            .where(PaymentMethod.provider == provider)
            .where(PaymentMethod.provider_token == token)
        )
