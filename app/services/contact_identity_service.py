"""Who wrote: an inbound sender's identifiers resolved to a contact (OMNI-001, OMNI-002).

The rule, and the whole of it (ADR-118): **identities link only by provider
assertion.** Two identifiers the provider named together in one signed payload -
a WhatsApp phone and business-scoped id - belong to one contact. Nothing else
links anything: not a matching display name, not an email, not a phone number
typed into a lead, not two contacts looking alike. Merging two contacts that
already exist is a human decision (O7), never an inference, so a payload that
names identifiers already held by *different* contacts is recorded as a conflict
and routed without merging.

Resolution, for one inbound message's sender:

1. Each identifier is looked up in its provider scope, inside the workspace.
2. **None known** - a new contact holding every identifier. A phone number also
   becomes the contact's `wa_id` (compatibility).
3. **All known to one contact** - that contact. Identifiers it does not yet hold
   are attached (`provider_pairing`), unless doing so would give it a second
   WhatsApp phone - then it is a conflict, and nothing is attached.
4. **Known to several contacts** - a conflict. The message goes to the contact
   holding the adapter's anchor kind (for WhatsApp the business-scoped id, which
   Meta sends on every message, so a thread does not flip between contacts as
   the phone comes and goes), and nothing moves.

**Races converge.** Two deliveries recording one person's first message both
miss the lookup and both try to create; the scoped-value unique rule lets one
win, and the loser's whole creation is unwound in a savepoint and resolved again
against the winner's rows. No duplicate contact survives either order.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import IdentityScopeRef
from app.channels.inbound import Identifier
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ContactIdentity,
    IdentityKind,
    IdentitySource,
)
from app.db.models.conversation import Contact
from app.repositories.channel_repository import IDENTITY_CONSTRAINT, ContactIdentityRepository
from app.repositories.conversation_repository import (
    CONTACT_IDENTITY_CONSTRAINT,
    ContactRepository,
)

logger = get_logger(__name__)

# How many times one sender is resolved again after losing a creation race. One
# retry settles every race two deliveries can run; the bound is for a database
# that keeps changing underneath, which is a fault to surface, not to loop on.
MAX_RESOLUTION_ATTEMPTS = 3


class _LostRaceError(Exception):
    """Another delivery created this sender first: unwind this one's creation."""


@dataclass(frozen=True, slots=True)
class SenderResolution:
    """The contact a message came from, and the identity it came through."""

    contact: Contact
    #: The identity a *new* conversation with this sender on this connection
    #: is pinned to. An existing conversation keeps the pin it already has.
    participant: ContactIdentity
    created_contact: bool = False
    #: Identities attached to an existing contact because the provider named
    #: them together with one it already held.
    paired: int = 0
    #: The provider named identifiers that already belong to different
    #: contacts. Recorded; never merged.
    conflict: bool = False


class ContactIdentityService:
    """Resolves senders to contacts for one workspace."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._identities = ContactIdentityRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)

    async def resolve_sender(
        self,
        *,
        connection: ChannelConnection,
        identifiers: Sequence[Identifier],
        scopes: Mapping[Identifier, IdentityScopeRef],
        participant_preference: Sequence[str],
        anchor_preference: Sequence[str],
        profile_name: str | None,
        seen_at: datetime,
    ) -> SenderResolution:
        """The contact and participant for a sender the provider identified by `identifiers`."""
        if not identifiers:
            # An adapter refuses a sender-less message before it gets here.
            raise ValueError("a sender needs at least one identifier")
        if connection.tenant_id != self._tenant_id:
            raise ValueError("the connection is not this workspace's")

        for _ in range(MAX_RESOLUTION_ATTEMPTS):
            try:
                resolution = await self._resolve_once(
                    connection=connection,
                    identifiers=identifiers,
                    scopes=scopes,
                    participant_preference=participant_preference,
                    anchor_preference=anchor_preference,
                )
            except _LostRaceError:
                continue
            self._refresh(resolution.contact, profile_name=profile_name, seen_at=seen_at)
            return resolution
        raise ConflictError("That customer could not be recorded.")

    async def _resolve_once(
        self,
        *,
        connection: ChannelConnection,
        identifiers: Sequence[Identifier],
        scopes: Mapping[Identifier, IdentityScopeRef],
        participant_preference: Sequence[str],
        anchor_preference: Sequence[str],
    ) -> SenderResolution:
        known: dict[Identifier, ContactIdentity] = {}
        for identifier in identifiers:
            scope = scopes[identifier]
            found = await self._identities.find(
                channel=connection.channel,
                kind=identifier.kind,
                scope=scope.scope,
                scope_ref=scope.scope_ref,
                value=identifier.value,
            )
            if found is not None:
                known[identifier] = found

        if not known:
            return await self._create(
                connection=connection,
                identifiers=identifiers,
                scopes=scopes,
                participant_preference=participant_preference,
            )

        owners = {identity.contact_id for identity in known.values()}
        conflict = len(owners) > 1
        contact_id = _anchored_owner(known, anchor_preference) if conflict else next(iter(owners))
        contact = await self._contacts.require_by_id(contact_id)

        paired = 0
        if not conflict:
            for identifier in identifiers:
                if identifier in known:
                    continue
                attached = await self._attach(
                    contact=contact, connection=connection, identifier=identifier, scopes=scopes
                )
                if attached is None:
                    conflict = True
                else:
                    known[identifier] = attached
                    paired += 1

        if conflict:
            logger.warning(
                "identity.pair_conflict",
                extra={
                    "event": "identity.pair_conflict",
                    "tenant_id": str(self._tenant_id),
                    "contact_id": str(contact.id),
                    "channel": connection.channel.value,
                },
            )

        mine = {
            identifier: identity
            for identifier, identity in known.items()
            if identity.contact_id == contact.id
        }
        return SenderResolution(
            contact=contact,
            participant=_preferred(mine, participant_preference),
            paired=paired,
            conflict=conflict,
        )

    async def _create(
        self,
        *,
        connection: ChannelConnection,
        identifiers: Sequence[Identifier],
        scopes: Mapping[Identifier, IdentityScopeRef],
        participant_preference: Sequence[str],
    ) -> SenderResolution:
        """A contact for a sender nobody has seen, holding every identifier the provider named.

        Inside one savepoint: a contact whose identity another delivery won is
        unwound whole, contact row included, and the sender resolved again.
        """
        phone = next(
            (
                identifier.value
                for identifier in identifiers
                if connection.channel is Channel.WHATSAPP and identifier.kind is IdentityKind.PHONE
            ),
            None,
        )
        try:
            async with self._session.begin_nested():
                contact = Contact(tenant_id=self._tenant_id, wa_id=phone)
                self._session.add(contact)
                await self._session.flush()
                held: dict[Identifier, ContactIdentity] = {}
                for identifier in identifiers:
                    scope = scopes[identifier]
                    identity, _ = await self._identities.insert(
                        contact_id=contact.id,
                        channel=connection.channel,
                        kind=identifier.kind,
                        scope=scope.scope,
                        scope_ref=scope.scope_ref,
                        connection_id=scope.connection_id,
                        value=identifier.value,
                        source=IdentitySource.PROVIDER,
                    )
                    # The phone identity may already exist - the contact's own
                    # `wa_id` trigger wrote it a statement ago - which is fine;
                    # one held by anybody else is the race this savepoint is for.
                    if identity is None or identity.contact_id != contact.id:
                        raise _LostRaceError()
                    held[identifier] = identity
        except IntegrityError as error:
            # Two first messages from one phone number: the contact row itself
            # lost on `uq_contacts_tenant_id_wa_id`, before any identity.
            if not _is_identity_race(error):
                raise
            raise _LostRaceError() from error
        return SenderResolution(
            contact=contact,
            participant=_preferred(held, participant_preference),
            created_contact=True,
        )

    async def _attach(
        self,
        *,
        contact: Contact,
        connection: ChannelConnection,
        identifier: Identifier,
        scopes: Mapping[Identifier, IdentityScopeRef],
    ) -> ContactIdentity | None:
        """Attach an identifier the provider paired with one this contact holds, or None.

        None when it cannot be attached without breaking a rule - the contact
        already has a different WhatsApp phone, or another contact won this
        identifier a moment ago - and the caller records a conflict.

        **A phone goes through `wa_id` first.** The contact's trigger then writes
        the phone identity, so this path takes the same locks in the same order
        as a new contact being created with that number: a first message from
        the number racing this pairing waits rather than deadlocking.
        """
        if connection.channel is Channel.WHATSAPP and identifier.kind is IdentityKind.PHONE:
            return await self._attach_phone(contact, identifier.value)
        scope = scopes[identifier]
        identity, _ = await self._identities.insert(
            contact_id=contact.id,
            channel=connection.channel,
            kind=identifier.kind,
            scope=scope.scope,
            scope_ref=scope.scope_ref,
            connection_id=scope.connection_id,
            value=identifier.value,
            source=IdentitySource.PROVIDER_PAIRING,
        )
        if identity is None or identity.contact_id != contact.id:
            return None
        return identity

    async def _attach_phone(self, contact: Contact, phone: str) -> ContactIdentity | None:
        if contact.wa_id is not None or await self._identities.phone_of(contact.id) is not None:
            # One WhatsApp phone per contact, and it is `wa_id`.
            return None
        try:
            async with self._session.begin_nested():
                contact.wa_id = phone
                await self._session.flush()
        except IntegrityError as error:
            if not _is_identity_race(error):
                raise
            # Somebody recorded this number on another contact between the
            # lookup and here. Resolve again against what they committed.
            raise _LostRaceError() from error
        identity = await self._identities.phone_of(contact.id)
        if identity is None or identity.value != phone:  # pragma: no cover - the trigger wrote it
            raise ConflictError("That customer's number could not be recorded.")
        identity.source = IdentitySource.PROVIDER_PAIRING
        return identity

    @staticmethod
    def _refresh(contact: Contact, *, profile_name: str | None, seen_at: datetime) -> None:
        """Keep what the provider told us current. An absent name never erases a known one."""
        if profile_name is not None:
            contact.display_name = profile_name
        if contact.last_seen_at is None or seen_at > contact.last_seen_at:
            contact.last_seen_at = seen_at


def _is_identity_race(error: IntegrityError) -> bool:
    """Whether an integrity failure is another delivery having recorded this person first."""
    text = str(error.orig)
    return CONTACT_IDENTITY_CONSTRAINT in text or IDENTITY_CONSTRAINT in text


def _preferred(
    held: Mapping[Identifier, ContactIdentity], preference: Sequence[str]
) -> ContactIdentity:
    """The identity a new conversation is pinned to: the adapter's first preference held."""
    for kind in preference:
        for identifier, identity in held.items():
            if identifier.kind.value == kind:
                return identity
    return next(iter(held.values()))


def _anchored_owner(
    known: Mapping[Identifier, ContactIdentity], anchor_preference: Sequence[str]
) -> uuid.UUID:
    """On a conflict, the contact holding the anchor kind - deterministic, never a merge."""
    for kind in anchor_preference:
        for identifier, identity in known.items():
            if identifier.kind.value == kind:
                return identity.contact_id
    return next(iter(known.values())).contact_id


__all__ = ["ContactIdentityService", "SenderResolution"]
