"""Data access for per-channel marketing consent (ENT-19).

Tenant-scoped: a consent row is keyed by the workspace, and every read and
write here names it. `refused` is the clause the campaign audience builds on,
so "opted out on this channel" is written once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent
from app.repositories.base import TenantScopedRepository


class ContactConsentRepository(TenantScopedRepository[ContactChannelConsent]):
    """One workspace's consent rows."""

    model = ContactChannelConsent

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        super().__init__(session, tenant_id=tenant_id)

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ContactChannelConsent.tenant_id == self.tenant_id

    async def get(self, contact_id: uuid.UUID, channel: Channel) -> ContactChannelConsent | None:
        return await self._first(
            self._select().where(
                ContactChannelConsent.contact_id == contact_id,
                ContactChannelConsent.channel == channel,
            )
        )

    async def lock(self, contact_id: uuid.UUID, channel: Channel) -> ContactChannelConsent:
        """The pair's row, created if absent, locked for this transaction.

        Insert-if-absent then `FOR UPDATE`, so two writers on the same pair -
        a stop word and the provider's preference record in one delivery, say
        - serialise on one row rather than racing to create two.
        """
        await self.session.flush()
        await self.session.execute(
            insert(ContactChannelConsent)
            .values(tenant_id=self.tenant_id, contact_id=contact_id, channel=channel)
            .on_conflict_do_nothing(constraint="pk_contact_channel_consents")
        )
        consent = await self._first(
            self._select()
            .where(
                ContactChannelConsent.contact_id == contact_id,
                ContactChannelConsent.channel == channel,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if consent is None:  # pragma: no cover - inserted or present above
            raise LookupError("the consent row vanished inside its own transaction")
        return consent

    async def for_contact(self, contact_id: uuid.UUID) -> list[ContactChannelConsent]:
        return await self._all(
            self._select()
            .where(ContactChannelConsent.contact_id == contact_id)
            .order_by(ContactChannelConsent.channel)
        )

    async def accepts_marketing(self, contact_id: uuid.UUID, channel: Channel) -> bool:
        """Whether marketing may reach this person on `channel`: no opt-out recorded there."""
        consent = await self.get(contact_id, channel)
        return consent is None or consent.accepts_marketing

    async def refusing(self, contact_ids: Iterable[uuid.UUID], channel: Channel) -> set[uuid.UUID]:
        """Which of these contacts have opted out of marketing on `channel`."""
        wanted = list(set(contact_ids))
        if not wanted:
            return set()
        rows = await self.session.scalars(
            select(ContactChannelConsent.contact_id).where(
                self._tenant_filter(),
                ContactChannelConsent.channel == channel,
                ContactChannelConsent.contact_id.in_(wanted),
                ContactChannelConsent.marketing_opt_out_at.is_not(None),
            )
        )
        return set(rows)


def refused(
    tenant_id: uuid.UUID,
    contact_id: SQLColumnExpression[uuid.UUID],
    channel: SQLColumnExpression[Channel] | Channel,
) -> ColumnElement[bool]:
    """SQL: this contact has opted out of marketing on this channel (ENT-19).

    For a query that selects contacts - the campaign audience - where the
    channel is the sending connection's.
    """
    return exists(
        select(ContactChannelConsent.contact_id).where(
            and_(
                ContactChannelConsent.tenant_id == tenant_id,
                ContactChannelConsent.contact_id == contact_id,
                ContactChannelConsent.channel == channel,
                ContactChannelConsent.marketing_opt_out_at.is_not(None),
            )
        )
    )


__all__ = ["ContactConsentRepository", "refused"]
