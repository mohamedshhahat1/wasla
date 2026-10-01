"""Data access for channel connections and contact identities.

`ConnectionDirectory` is the one deliberately unscoped class here, for the reason
`WhatsAppAccountDirectory` is: resolving a provider's connection identifier is
how an inbound event's workspace is discovered in the first place, so it cannot
be workspace-scoped. It is keyed on the **connection** - never on anything the
sender controls - and it answers "who held this connection when the event
happened", not "who holds it now" (ADR-101). Everything else is tenant-scoped.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.channels.throughput import SEND_WINDOW
from app.core.logging import get_logger
from app.db.models.channel import (
    MAX_HEALTH_REASON_LENGTH,
    Channel,
    ChannelConnection,
    ConnectionHealth,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = get_logger(__name__)

# The same bounded reasons the WhatsApp directory writes, so an operator reading
# an event's error sees one vocabulary whatever the channel.
UNKNOWN_CONNECTION = "unknown_number"
LATE_UNOWNED = "historical_owner_unknown"
AMBIGUOUS_OWNERSHIP = "ambiguous_ownership"

# How many claims on one connection the historical walk will look at (ADR-101).
MAX_OWNERSHIP_HISTORY = 50

# The unique rule an identity insert races on. Named so a conflict on it is read
# as a race and any other integrity failure as the bug it is.
IDENTITY_CONSTRAINT = "uq_contact_identities_scoped_value"


@dataclass(frozen=True, slots=True)
class ConnectionResolution:
    """Which workspace's connection an event belongs to, or why none does.

    The three failures stay apart for the reason `OwnershipResolution` keeps
    them apart: not ours, ours but nobody's at that instant, and a broken
    invariant are three different things for an operator.
    """

    connection: ChannelConnection | None
    reason: str | None = None

    @classmethod
    def held_by(cls, connection: ChannelConnection) -> ConnectionResolution:
        return cls(connection=connection)

    @classmethod
    def unresolved(cls, reason: str) -> ConnectionResolution:
        return cls(connection=None, reason=reason)


class ConnectionDirectory(BaseRepository[ChannelConnection]):
    """Resolves a provider connection to the workspace that held it. Unscoped by necessity."""

    model = ChannelConnection

    async def live(self, channel: Channel, external_account_id: str) -> ChannelConnection | None:
        """The workspace that holds this connection now, if any. Not how events are routed."""
        return await self._first(
            self._select().where(
                ChannelConnection.channel == channel,
                ChannelConnection.external_account_id == external_account_id,
                ChannelConnection.released_at.is_(None),
            )
        )

    async def holders_of(
        self, channel: Channel, external_account_id: str
    ) -> list[ChannelConnection]:
        """Every claim this connection has carried, newest first, bounded."""
        return await self._all(
            self._select()
            .where(
                ChannelConnection.channel == channel,
                ChannelConnection.external_account_id == external_account_id,
            )
            .order_by(ChannelConnection.ownership_started_at.desc(), ChannelConnection.id.desc())
            .limit(MAX_OWNERSHIP_HISTORY)
        )

    async def owner_at(
        self,
        channel: Channel,
        external_account_id: str,
        instant: datetime,
        *,
        live: ChannelConnection | None = None,
    ) -> ConnectionResolution:
        """The workspace that held this connection when the event happened (ADR-101).

        Never falls back to the current holder across a handover: a message
        with no establishable owner is dropped and counted, because handing it
        to a stranger cannot be undone. An instant outside every claim is
        attributed to the live claim only when every claim this connection has
        ever carried is that same workspace's - then nobody else can be owed it.
        """
        current = live if live is not None else await self.live(channel, external_account_id)
        if current is not None and current.held_at(instant):
            return ConnectionResolution.held_by(current)

        history = await self.holders_of(channel, external_account_id)
        if not history:
            return ConnectionResolution.unresolved(UNKNOWN_CONNECTION)

        matches = [connection for connection in history if connection.held_at(instant)]
        if len(matches) == 1:
            return ConnectionResolution.held_by(matches[0])
        if matches:
            logger.error(
                "channel.ambiguous_connection_ownership",
                extra={
                    "event": "channel.ambiguous_connection_ownership",
                    "channel": channel.value,
                    "claims": len(matches),
                },
            )
            return ConnectionResolution.unresolved(AMBIGUOUS_OWNERSHIP)

        if current is not None and all(
            connection.tenant_id == current.tenant_id for connection in history
        ):
            return ConnectionResolution.held_by(current)
        return ConnectionResolution.unresolved(LATE_UNOWNED)


class ChannelConnectionRepository(TenantScopedRepository[ChannelConnection]):
    """Connections of one workspace."""

    model = ChannelConnection

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ChannelConnection.tenant_id == self.tenant_id

    async def get_by_id(self, connection_id: uuid.UUID) -> ChannelConnection | None:
        return await self._first(self._select().where(ChannelConnection.id == connection_id))

    async def require_by_id(
        self, connection_id: uuid.UUID, *, fresh: bool = True
    ) -> ChannelConnection:
        """This workspace's connection, re-read by default.

        Fresh because the WhatsApp mirror updates this row from a trigger, behind
        the identity map's back: a number disabled a moment ago must read as
        disabled here, not as the copy this session loaded earlier.
        """
        statement = self._select().where(ChannelConnection.id == connection_id)
        if fresh:
            statement = statement.execution_options(populate_existing=True)
        return await self._require(statement)

    async def list_all(
        self, *, channel: Channel | None = None, limit: int = 100
    ) -> list[ChannelConnection]:
        statement = (
            self._select()
            .where(ChannelConnection.released_at.is_(None))
            .order_by(ChannelConnection.created_at.desc(), ChannelConnection.id.desc())
            .limit(limit)
        )
        if channel is not None:
            statement = statement.where(ChannelConnection.channel == channel)
        return await self._all(statement)

    async def record_health(
        self,
        connection_id: uuid.UUID,
        health: ConnectionHealth,
        *,
        reason: str | None = None,
    ) -> bool:
        """Record what the provider said about using this connection. Returns whether it changed.

        A conditional UPDATE, so a healthy connection sending thousands of
        messages writes nothing - and takes no row lock - on any of them; only a
        change of state is a write (OMNI-012).
        """
        result = await self.session.execute(
            update(ChannelConnection)
            .where(
                self._tenant_filter(),
                ChannelConnection.id == connection_id,
                ChannelConnection.health != health,
            )
            .values(
                health=health,
                health_reason=reason[:MAX_HEALTH_REASON_LENGTH] if reason else None,
                health_changed_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        return bool(getattr(result, "rowcount", 0))

    async def take_send_allowance(
        self,
        connection_id: uuid.UUID,
        *,
        per_window: int,
        now: datetime,
        may_refuse: bool = True,
    ) -> datetime | None:
        """Admit one send against this connection's allowance, or say when to come back.

        Returns None when the send is admitted and the instant the current
        window reopens when it is not (ADR-123). One conditional UPDATE: a
        window that has run out restarts at `now` with this send as its first,
        and a live one admits while its count is under `per_window`. Concurrent
        callers queue on the row for one statement and re-read it, so the count
        cannot pass the allowance. A refusal writes nothing.

        `may_refuse=False` counts the send whatever the count: a reply to a
        customer spends allowance, so bulk senders see it, and is never refused.
        """
        started = ChannelConnection.send_window_started_at
        expired = or_(started.is_(None), started <= now - SEND_WINDOW)
        conditions = [self._tenant_filter(), ChannelConnection.id == connection_id]
        if may_refuse:
            conditions.append(or_(expired, ChannelConnection.send_window_count < per_window))
        admitted = await self.session.execute(
            update(ChannelConnection)
            .where(*conditions)
            .values(
                send_window_started_at=case((expired, now), else_=started),
                send_window_count=case((expired, 1), else_=ChannelConnection.send_window_count + 1),
            )
            .returning(ChannelConnection.id)
            .execution_options(synchronize_session=False)
        )
        if admitted.scalar_one_or_none() is not None:
            return None
        opened = await self.session.scalar(
            select(started).where(self._tenant_filter(), ChannelConnection.id == connection_id)
        )
        return (opened or now) + SEND_WINDOW


class ContactIdentityRepository(TenantScopedRepository[ContactIdentity]):
    """How providers address one workspace's contacts."""

    model = ContactIdentity

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ContactIdentity.tenant_id == self.tenant_id

    async def get_by_id(self, identity_id: uuid.UUID) -> ContactIdentity | None:
        return await self._first(self._select().where(ContactIdentity.id == identity_id))

    async def require_by_id(self, identity_id: uuid.UUID) -> ContactIdentity:
        return await self._require(self._select().where(ContactIdentity.id == identity_id))

    async def find(
        self,
        *,
        channel: Channel,
        kind: IdentityKind,
        scope: IdentityScope,
        scope_ref: str,
        value: str,
    ) -> ContactIdentity | None:
        """The identity with exactly this scoped value, in this workspace, if one exists."""
        return await self._first(
            self._select().where(
                ContactIdentity.channel == channel,
                ContactIdentity.kind == kind,
                ContactIdentity.scope == scope,
                ContactIdentity.scope_ref == scope_ref,
                ContactIdentity.value == value,
            )
        )

    async def map_by_ids(
        self, identity_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ContactIdentity]:
        """These identities of this workspace, by id, in one query."""
        if not identity_ids:
            return {}
        rows = await self._all(self._select().where(ContactIdentity.id.in_(identity_ids)))
        return {row.id: row for row in rows}

    async def map_for_contacts(
        self, contact_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[ContactIdentity]]:
        """Every identity of these contacts, by contact, oldest first, in one query."""
        if not contact_ids:
            return {}
        rows = await self._all(
            self._select()
            .where(ContactIdentity.contact_id.in_(contact_ids))
            .order_by(ContactIdentity.created_at, ContactIdentity.id)
        )
        grouped: dict[uuid.UUID, list[ContactIdentity]] = {}
        for row in rows:
            grouped.setdefault(row.contact_id, []).append(row)
        return grouped

    async def list_for_contact(self, contact_id: uuid.UUID) -> list[ContactIdentity]:
        return await self._all(
            self._select()
            .where(ContactIdentity.contact_id == contact_id)
            .order_by(ContactIdentity.created_at, ContactIdentity.id)
        )

    async def phone_of(self, contact_id: uuid.UUID) -> ContactIdentity | None:
        """The contact's WhatsApp phone identity: at most one, by a unique index."""
        return await self._first(
            self._select().where(
                ContactIdentity.contact_id == contact_id,
                ContactIdentity.channel == Channel.WHATSAPP,
                ContactIdentity.kind == IdentityKind.PHONE,
            )
        )

    async def insert(
        self,
        *,
        contact_id: uuid.UUID,
        channel: Channel,
        kind: IdentityKind,
        scope: IdentityScope,
        scope_ref: str,
        connection_id: uuid.UUID | None,
        value: str,
        source: IdentitySource,
    ) -> tuple[ContactIdentity | None, bool]:
        """Attach an identity to a contact, or report who already holds it.

        Returns the identity now holding this scoped value and whether this
        call created it. `ON CONFLICT DO NOTHING` on the scoped-value rule makes
        two deliveries racing to record one person's first message converge on
        one row rather than one of them failing: the loser reads the winner
        back, and the caller decides what that means (ADR-118).
        """
        statement = (
            pg_insert(ContactIdentity)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                contact_id=contact_id,
                channel=channel,
                kind=kind,
                scope=scope,
                scope_ref=scope_ref,
                connection_id=connection_id,
                value=value,
                source=source,
            )
            .on_conflict_do_nothing(constraint=IDENTITY_CONSTRAINT)
            .returning(ContactIdentity.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        found = await self._first(
            self._select()
            .where(
                ContactIdentity.channel == channel,
                ContactIdentity.kind == kind,
                ContactIdentity.scope == scope,
                ContactIdentity.scope_ref == scope_ref,
                ContactIdentity.value == value,
            )
            .execution_options(populate_existing=True)
        )
        return found, inserted is not None


__all__ = [
    "AMBIGUOUS_OWNERSHIP",
    "LATE_UNOWNED",
    "UNKNOWN_CONNECTION",
    "ChannelConnectionRepository",
    "ConnectionDirectory",
    "ConnectionResolution",
    "ContactIdentityRepository",
]
