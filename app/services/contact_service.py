"""A contact as a member reads it: who they are, the name the team gave, their consent history.

FEB-19 (CD-03 .. CD-09). A contact is still created only by the webhook, from
what the channel reports, and its identities are still never typed or edited
(CRM-20, ADR-122). What a person may change is one thing: the name the team
calls the customer by, kept apart from the channel's own profile name and shown
before it wherever a customer is named.

The consent history is read here and written elsewhere: `app.services.opt_out`
is the one writer of an opt-out and of its lifting, and it stages the history
row in the transaction that changes the state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.pagination import Cursor, Page
from app.db.models import User
from app.db.models.audit import AuditAction
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel, ContactIdentity
from app.db.models.consent import ConsentAction, ContactChannelConsent, ContactConsentEvent
from app.db.models.conversation import Contact, Conversation
from app.repositories.channel_repository import ContactIdentityRepository
from app.repositories.consent_repository import ContactConsentRepository
from app.repositories.conversation_repository import ContactRepository
from app.services.audit_service import AuditTrail

#: The stream's name for "who this conversation is with changed" (CD-09).
PARTICIPANT_FIELD = "participant"


@dataclass(frozen=True, slots=True)
class ContactView:
    """One contact with everything its read shows."""

    contact: Contact
    identities: list[ContactIdentity]
    consents: list[ContactChannelConsent]
    #: The newest inbound message across the contact's conversations.
    last_inbound_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConsentEventView:
    """One row of a contact's consent history.

    `id` is None for the one kind of row that is not stored: an opt-out
    recorded before the history was kept, read from the state's own timestamp
    (`before_history`).
    """

    id: uuid.UUID | None
    channel: Channel
    action: ConsentAction
    source: OptOutSource | None
    via: OptOutVia | None
    actor_user_id: uuid.UUID | None
    actor_name: str | None
    at: datetime
    before_history: bool = False


class ContactService:
    """Contact reads, and the one edit, for one workspace."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._identities = ContactIdentityRepository(session, tenant_id=tenant_id)
        self._consents = ContactConsentRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def read(self, contact_id: uuid.UUID) -> ContactView:
        """The contact, or the generic not-found another workspace's id also gets."""
        contact = await self._contacts.require_by_id(contact_id)
        return await self._view(contact)

    async def rename(self, contact_id: uuid.UUID, *, name: str | None, actor: User) -> ContactView:
        """Give the customer a name, change it, or take it away (`None`).

        An unchanged value writes nothing - no row, no audit entry, no frame
        (CD-04). A change is one UPDATE, one audit entry holding both values,
        and one `conversation.updated` per conversation of the contact, staged
        on the transaction so open inboxes re-read who they are talking to
        (CD-09). Last writer wins: one field, no precondition.
        """
        contact = await self._contacts.require_by_id(contact_id)
        if contact.name == name:
            return await self._view(contact)
        previous = contact.name
        contact.name = name
        self._audit.record(
            AuditAction.CONTACT_RENAMED,
            actor=actor,
            target_type="contact",
            target_id=contact.id,
            meta={"from": previous, "to": name},
        )
        conversation_ids = await self._session.scalars(
            select(Conversation.id).where(
                Conversation.tenant_id == self._tenant_id,
                Conversation.contact_id == contact.id,
            )
        )
        for conversation_id in conversation_ids:
            events.stage(
                self._session.sync_session,
                events.RealtimeEvent(
                    tenant_id=self._tenant_id,
                    type="conversation.updated",
                    data={
                        "conversation_id": str(conversation_id),
                        "fields": [PARTICIPANT_FIELD],
                    },
                ),
            )
        await self._session.flush()
        return await self._view(contact)

    async def consent_events(
        self, contact_id: uuid.UUID, *, limit: int, cursor: str | None
    ) -> Page[ConsentEventView]:
        """The contact's consent history, newest first (CD-06).

        On the last page, an opt-out that stands today with no stored event -
        one older than the history - closes the list, one row per channel,
        from the state's own timestamp. Never stored.
        """
        contact = await self._contacts.require_by_id(contact_id)
        after = Cursor.decode(cursor) if cursor else None
        statement = (
            select(ContactConsentEvent, User.full_name)
            .outerjoin(User, User.id == ContactConsentEvent.actor_user_id)
            .where(
                ContactConsentEvent.tenant_id == self._tenant_id,
                ContactConsentEvent.contact_id == contact.id,
            )
            .order_by(ContactConsentEvent.created_at.desc(), ContactConsentEvent.id.desc())
            .limit(limit)
        )
        if after is not None and after.sort_value is not None:
            statement = statement.where(
                or_(
                    ContactConsentEvent.created_at < after.sort_value,
                    and_(
                        ContactConsentEvent.created_at == after.sort_value,
                        ContactConsentEvent.id < after.id,
                    ),
                )
            )
        rows = (await self._session.execute(statement)).all()
        items = [
            ConsentEventView(
                id=event.id,
                channel=event.channel,
                action=event.action,
                source=event.source,
                via=event.via,
                actor_user_id=event.actor_user_id,
                actor_name=name,
                at=event.created_at,
            )
            for event, name in rows
        ]
        if len(rows) == limit:
            last = rows[-1][0]
            return Page(
                items=items, next_cursor=Cursor(sort_value=last.created_at, id=last.id).encode()
            )
        items.extend(await self._before_history(contact.id))
        return Page(items=items, next_cursor=None)

    async def _before_history(self, contact_id: uuid.UUID) -> list[ConsentEventView]:
        """Standing opt-outs no stored event accounts for, newest first."""
        remembered = set(
            await self._session.scalars(
                select(ContactConsentEvent.channel)
                .where(
                    ContactConsentEvent.tenant_id == self._tenant_id,
                    ContactConsentEvent.contact_id == contact_id,
                    ContactConsentEvent.action == ConsentAction.OPTED_OUT,
                )
                .distinct()
            )
        )
        older = [
            consent
            for consent in await self._consents.for_contact(contact_id)
            if consent.marketing_opt_out_at is not None and consent.channel not in remembered
        ]
        older.sort(key=lambda consent: consent.marketing_opt_out_at or datetime.min, reverse=True)
        return [
            ConsentEventView(
                id=None,
                channel=consent.channel,
                action=ConsentAction.OPTED_OUT,
                source=None,
                via=None,
                actor_user_id=None,
                actor_name=None,
                at=consent.marketing_opt_out_at,  # type: ignore[arg-type]
                before_history=True,
            )
            for consent in older
        ]

    async def _view(self, contact: Contact) -> ContactView:
        identities = await self._identities.list_for_contact(contact.id)
        consents = await self._consents.for_contact(contact.id)
        # One aggregate, whatever the number of conversations (CD-03): never a
        # read per conversation.
        last_inbound_at = await self._session.scalar(
            select(func.max(Conversation.last_inbound_at)).where(
                Conversation.tenant_id == self._tenant_id,
                Conversation.contact_id == contact.id,
            )
        )
        return ContactView(
            contact=contact,
            identities=identities,
            consents=consents,
            last_inbound_at=last_inbound_at,
        )


__all__ = ["PARTICIPANT_FIELD", "ConsentEventView", "ContactService", "ContactView"]
