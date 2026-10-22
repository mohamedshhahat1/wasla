"""The campaigns page's contact picker (PG-07).

One workspace's contacts, newest activity first, a page at a time:

- **on one number** (`account_id`): contacts with a conversation on it,
  ordered by that conversation's last inbound message - the order a person
  scanning "who wrote to us lately" expects - with whether each one could be
  sent a campaign from that number;
- **on none**: every contact, ordered by when it was last seen.

Never another workspace's: every statement starts from this workspace's rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Select, SQLColumnExpression, and_, or_, select, text
from sqlalchemy.orm import aliased

from app.core.pagination import Cursor
from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact, Conversation
from app.db.models.lead import Lead, LeadStatus
from app.repositories.base import TenantScopedRepository, escape_like
from app.repositories.consent_repository import refused

#: Why a contact on the number asked about cannot be sent a campaign from it.
#: Everybody listed on a number has a conversation on it, so the one reason
#: left is a refusal on that number's channel (ENT-19).
OPTED_OUT: Final = "opted_out"


@dataclass(frozen=True, slots=True)
class PickedContact:
    """One row of the picker."""

    id: uuid.UUID
    display_name: str | None
    wa_id: str | None
    lead_id: uuid.UUID | None
    lead_name: str | None
    lead_status: LeadStatus | None
    last_inbound_at: datetime | None
    opted_out_at: datetime | None
    #: Null when no number was named: eligibility is a property of a number.
    eligible_for_campaign: bool | None
    ineligible_reason: str | None


def _after(
    column: SQLColumnExpression[datetime | None],
    key: SQLColumnExpression[uuid.UUID],
    after: Cursor,
) -> ColumnElement[bool]:
    """Rows after `after` under `ORDER BY column DESC NULLS LAST, key DESC`."""
    if after.sort_value is None:
        return and_(column.is_(None), key < after.id)
    return or_(
        column < after.sort_value,
        and_(column == after.sort_value, key < after.id),
        column.is_(None),
    )


class ContactPickerRepository(TenantScopedRepository[Contact]):
    """Contacts of one workspace, for choosing people by hand."""

    model = Contact

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Contact.tenant_id == self.tenant_id

    def _search(self, statement: Select, q: str | None) -> Select:  # type: ignore[type-arg]
        if not q:
            return statement
        digits = q.lstrip("+").replace(" ", "")
        if digits.isdigit():
            return statement.where(Contact.wa_id.ilike(f"%{escape_like(digits)}%", escape="\\"))
        pattern = f"%{escape_like(q)}%"
        return statement.where(
            or_(
                Contact.display_name.ilike(pattern, escape="\\"),
                Lead.name.ilike(pattern, escape="\\"),
            )
        )

    def _active_lead(self) -> ColumnElement[bool]:
        # The contact's active lead: `uq_leads_active_contact`'s predicate.
        return and_(
            Lead.tenant_id == self.tenant_id,
            Lead.contact_id == Contact.id,
            Lead.status.not_in([LeadStatus.WON, LeadStatus.LOST]),
        )

    async def page(
        self,
        *,
        account_id: uuid.UUID | None,
        q: str | None,
        eligible_only: bool,
        limit: int,
        after: Cursor | None,
    ) -> list[tuple[PickedContact, Cursor]]:
        """One page and each row's cursor, newest first.

        **A custom plan, every time** - the knowledge search's lesson (docs/
        RAG.md). The statement is prepared once per pooled connection, and after
        five executions PostgreSQL weighs a generic plan built without the
        search text or the workspace. Measured at 100,000 contacts: a name
        search took 15 ms five times and 5.3 s every time after.
        """
        await self.session.execute(
            text("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
        )
        if account_id is not None:
            return await self._on_number(
                account_id, q=q, eligible_only=eligible_only, limit=limit, after=after
            )
        return await self._anywhere(q=q, limit=limit, after=after)

    async def _on_number(
        self,
        account_id: uuid.UUID,
        *,
        q: str | None,
        eligible_only: bool,
        limit: int,
        after: Cursor | None,
    ) -> list[tuple[PickedContact, Cursor]]:
        # Aliased: `refused` below reads the same table, and must not be
        # correlated with the row this join shows.
        shown = aliased(ContactChannelConsent)
        consent = and_(
            shown.tenant_id == self.tenant_id,
            shown.contact_id == Contact.id,
            shown.channel == Conversation.channel,
        )
        statement = (
            select(
                Contact.id,
                Contact.display_name,
                Contact.wa_id,
                Lead.id,
                Lead.name,
                Lead.status,
                Conversation.last_inbound_at,
                shown.marketing_opt_out_at,
            )
            .select_from(Conversation)
            .join(
                Contact,
                and_(Contact.id == Conversation.contact_id, Contact.tenant_id == self.tenant_id),
            )
            .outerjoin(Lead, self._active_lead())
            .outerjoin(shown, consent)
            .where(Conversation.tenant_id == self.tenant_id, Conversation.account_id == account_id)
        )
        if eligible_only:
            # The audience's own base-population rule (`refused`, ENT-19), as an
            # anti-join: filtering the outer join's null instead made the planner
            # hash every contact of the number (153 ms at 100,000; this, 1 ms).
            statement = statement.where(~refused(self.tenant_id, Contact.id, Conversation.channel))
        statement = self._search(statement, q)
        if after is not None:
            statement = statement.where(_after(Conversation.last_inbound_at, Contact.id, after))
        rows = await self.session.execute(
            statement.order_by(
                Conversation.last_inbound_at.desc().nulls_last(), Contact.id.desc()
            ).limit(limit)
        )
        picked: list[tuple[PickedContact, Cursor]] = []
        for contact_id, name, wa_id, lead_id, lead_name, status, inbound, opted in rows.all():
            picked.append(
                (
                    PickedContact(
                        id=contact_id,
                        display_name=name,
                        wa_id=wa_id,
                        lead_id=lead_id,
                        lead_name=lead_name,
                        lead_status=status,
                        last_inbound_at=inbound,
                        opted_out_at=opted,
                        eligible_for_campaign=opted is None,
                        ineligible_reason=OPTED_OUT if opted is not None else None,
                    ),
                    Cursor(sort_value=inbound, id=contact_id),
                )
            )
        return picked

    async def _anywhere(
        self, *, q: str | None, limit: int, after: Cursor | None
    ) -> list[tuple[PickedContact, Cursor]]:
        whatsapp_opt_out = (
            select(ContactChannelConsent.marketing_opt_out_at)
            .where(
                ContactChannelConsent.tenant_id == self.tenant_id,
                ContactChannelConsent.contact_id == Contact.id,
                ContactChannelConsent.channel == Channel.WHATSAPP,
            )
            .scalar_subquery()
        )
        statement = (
            select(
                Contact.id,
                Contact.display_name,
                Contact.wa_id,
                Lead.id,
                Lead.name,
                Lead.status,
                Contact.last_seen_at,
                whatsapp_opt_out,
            )
            .select_from(Contact)
            .outerjoin(Lead, self._active_lead())
            .where(self._tenant_filter())
        )
        statement = self._search(statement, q)
        if after is not None:
            statement = statement.where(_after(Contact.last_seen_at, Contact.id, after))
        rows = await self.session.execute(
            statement.order_by(Contact.last_seen_at.desc().nulls_last(), Contact.id.desc()).limit(
                limit
            )
        )
        return [
            (
                PickedContact(
                    id=contact_id,
                    display_name=name,
                    wa_id=wa_id,
                    lead_id=lead_id,
                    lead_name=lead_name,
                    lead_status=status,
                    last_inbound_at=seen,
                    opted_out_at=opted,
                    eligible_for_campaign=None,
                    ineligible_reason=None,
                ),
                Cursor(sort_value=seen, id=contact_id),
            )
            for contact_id, name, wa_id, lead_id, lead_name, status, seen, opted in rows.all()
        ]


__all__ = ["OPTED_OUT", "ContactPickerRepository", "PickedContact"]
