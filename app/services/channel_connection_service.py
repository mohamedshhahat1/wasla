"""Connecting, enabling, disabling and releasing a channel connection of any channel (ENT-07).

The neutral half of a connection's lifecycle, and the one every future adapter's
connect flow must use. WhatsApp keeps its own connect flow - a number is claimed
by proving control of it to Meta (`WhatsAppAccountService`) - and both run the
same `ChannelCapacityGuard`, so there is one answer to "may this connection be
active" whichever channel asks.

**What takes a slot** (ENT-07):

| State | Takes a slot | How it gets there |
| --- | --- | --- |
| being connected | no | a connect flow in progress, no row yet |
| active | **yes** | connected, or enabled again |
| disabled | no | `disable` - claim, credential and history kept |
| released | no | `release` - claim ended, history kept |

Disabling and releasing are never refused, at or over capacity alike. Enabling a
disabled connection, and connecting one, need a free slot - the guard answers
under the workspace's lock in the same transaction. Nothing here deletes a
connection: there is no hard delete, and history is never destroyed.

**During the compatibility window a WhatsApp number's lifecycle is written on
`whatsapp_accounts`** and mirrored onto its connection by a trigger (ADR-117).
`disable` and `enable` therefore write the number's status for a WhatsApp
connection and the connection's own for any other - and the disable provenance
(`disabled_reason`, `disabled_at`, `disabled_by`) on the connection either way,
where the mirror never touches it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.policy import ChannelState
from app.channels.registry import ChannelRegistry, ChannelUnavailableError, default_registry
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionStatus,
)
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.channel_repository import ChannelConnectionRepository, ConnectionDirectory
from app.services.audit_service import AuditTrail
from app.services.channel_capacity import ChannelCapacityGuard, ChannelSlot

logger = get_logger(__name__)

# The partial unique index that makes one live claim per provider connection.
LIVE_CONNECTION_INDEX = "uq_channel_connections_live_external_account"


class ChannelConnectionService:
    """One workspace's connections, whatever their channel."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_plan_code: str | None,
        registry: ChannelRegistry | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default_plan_code = default_plan_code
        self._registry = registry
        self._connections = ChannelConnectionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    def _guard(self) -> ChannelCapacityGuard:
        return ChannelCapacityGuard(
            self._session, tenant_id=self._tenant_id, default_plan_code=self._default_plan_code
        )

    # ------------------------------------------------------------ connect

    async def connect(
        self,
        *,
        channel: Channel,
        external_account_id: str,
        actor: User | None = None,
        ownership_verified_at: datetime | None = None,
    ) -> ChannelConnection:
        """Make a new active connection on `channel`, having asked the guard twice.

        For an adapter's connect flow, which proves the workspace controls the
        provider account before calling this (its own proof, like WhatsApp's).
        Refused: WhatsApp, whose numbers are claimed only with proof of
        ownership through `WhatsAppAccountService`; a channel Wasla does not
        operate (`ChannelUnavailableError`); no free slot or a type the plan
        does not include (409, from the guard, before the channel is asked
        anything); and a provider account another workspace holds (409).
        """
        if channel is Channel.WHATSAPP:
            raise ValidationError(
                "A WhatsApp number is connected with proof of ownership; use the WhatsApp "
                "connect flow."
            )
        external = external_account_id.strip()
        if not external:
            raise ValidationError("A connection names the provider account it is for.")
        guard = self._guard()
        await guard.precheck(channel)
        registry = self._registry or default_registry()
        if registry.state_for(channel) is not ChannelState.OPERATIONAL:
            # No adapter, or paused: nothing could carry this connection's
            # traffic, and a connection nothing can operate is not connected.
            raise ChannelUnavailableError()
        if await ConnectionDirectory(self._session).live(channel, external) is not None:
            raise ConflictError("That account is already connected.")
        await guard.reserve_or_refuse(channel)
        moment = datetime.now(UTC)
        connection = ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=self._tenant_id,
            channel=channel,
            external_account_id=external,
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=moment,
            ownership_verified_at=ownership_verified_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(connection)
                await self._session.flush()
        except IntegrityError as error:
            if LIVE_CONNECTION_INDEX not in str(error.orig):
                raise
            raise ConflictError("That account is already connected.") from error
        self._record(AuditAction.CHANNEL_CONNECTION_CONNECTED, connection, actor=actor)
        logger.info(
            "channel.connection_connected",
            extra={
                "event": "channel.connection_connected",
                "tenant_id": str(self._tenant_id),
                "channel": channel.value,
            },
        )
        return connection

    # ---------------------------------------------------- enable / disable

    async def enable(
        self, connection_id: uuid.UUID, *, actor: User | None = None
    ) -> ChannelConnection:
        """Make a disabled connection active again - which needs a free slot (ENT-07).

        It freed its slot when it was disabled, so the guard is asked, under the
        workspace's lock, before it is given one back; 409 when there is none.
        An already active connection changes nothing and asks nothing. A
        released one is not found: taking a released account back is a new
        connect, with the provider's proof, like anybody else's.
        """
        connection = await self._live(connection_id)
        if connection.status is not ConnectionStatus.ACTIVE:
            slot = await self._guard().reserve_or_refuse(connection.channel)
            await self._write_status(connection, ConnectionStatus.ACTIVE, slot=slot)
        connection.disabled_reason = None
        connection.disabled_at = None
        connection.disabled_by = None
        await self._session.flush()
        self._record(AuditAction.CHANNEL_CONNECTION_ENABLED, connection, actor=actor)
        return await self._connections.require_by_id(connection.id)

    async def disable(
        self,
        connection_id: uuid.UUID,
        *,
        reason: ConnectionDisabledReason = ConnectionDisabledReason.MANUAL,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
        meta: dict[str, object] | None = None,
    ) -> ChannelConnection:
        """Stop a connection's traffic and free its slot. Never refused (ENT-07).

        The claim, the credential, the conversations and the contacts stay; an
        owner may enable it again when a slot is free. Disabling an already
        disabled connection only records the newer reason.
        """
        connection = await self._live(connection_id)
        await self._write_status(connection, ConnectionStatus.DISABLED)
        connection.disabled_reason = reason
        connection.disabled_at = datetime.now(UTC)
        connection.disabled_by = actor.id if actor is not None else None
        await self._session.flush()
        self._record(
            AuditAction.CHANNEL_CONNECTION_DISABLED,
            connection,
            actor=actor,
            actor_kind=actor_kind,
            meta={"reason": reason.value, **(meta or {})},
        )
        return await self._connections.require_by_id(connection.id)

    async def release(
        self, connection_id: uuid.UUID, *, actor: User | None = None
    ) -> ChannelConnection:
        """Give a non-WhatsApp connection up: the claim ends, the history stays.

        A WhatsApp number is released by `WhatsAppAccountService.release`, which
        also drops its stored credential.
        """
        connection = await self._live(connection_id)
        if connection.channel is Channel.WHATSAPP:
            raise ValidationError("A WhatsApp number is released through the WhatsApp flow.")
        connection.status = ConnectionStatus.RELEASED
        connection.released_at = datetime.now(UTC)
        await self._session.flush()
        self._record(AuditAction.CHANNEL_CONNECTION_RELEASED, connection, actor=actor)
        return connection

    # ------------------------------------------------------------- reads

    async def list_connections(self, *, channel: Channel | None = None) -> list[ChannelConnection]:
        return await self._connections.list_all(channel=channel)

    # ----------------------------------------------------------- helpers

    async def _live(self, connection_id: uuid.UUID) -> ChannelConnection:
        connection = await self._connections.require_by_id(connection_id)
        if connection.released_at is not None:
            # Released rows keep their history and are not found here: enabling
            # or disabling a claim the workspace gave up would act on an account
            # somebody else may hold now.
            raise NotFoundError("No such connection.")
        return connection

    async def _write_status(
        self,
        connection: ChannelConnection,
        status: ConnectionStatus,
        *,
        slot: ChannelSlot | None = None,
    ) -> None:
        """Write the lifecycle where it lives: the number for WhatsApp, else the connection.

        Making a connection active takes the guard's `slot` for it, so no caller
        can activate one the guard was not asked about (ENT-08).
        """
        if status is ConnectionStatus.ACTIVE and (
            slot is None
            or slot.tenant_id != self._tenant_id
            or slot.channel is not connection.channel
        ):
            raise ValueError("Activating a connection takes the guard's slot for its channel.")
        if connection.channel is Channel.WHATSAPP:
            account = await self._session.get(WhatsAppAccount, connection.id)
            if account is None or account.tenant_id != self._tenant_id:  # pragma: no cover
                raise ValidationError("This WhatsApp connection has no number row.")
            account.status = WhatsAppAccountStatus(status.value)
            await self._session.flush()
            return
        connection.status = status

    def _record(
        self,
        action: AuditAction,
        connection: ChannelConnection,
        *,
        actor: User | None,
        actor_kind: AuditActorKind | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        if connection.channel is Channel.WHATSAPP and action in (
            AuditAction.CHANNEL_CONNECTION_ENABLED,
            AuditAction.CHANNEL_CONNECTION_DISABLED,
        ):
            # A number's lifecycle keeps the actions it has always had.
            action = (
                AuditAction.WHATSAPP_ACCOUNT_ENABLED
                if action is AuditAction.CHANNEL_CONNECTION_ENABLED
                else AuditAction.WHATSAPP_ACCOUNT_DISABLED
            )
        self._audit.record(
            action,
            actor=actor,
            actor_kind=actor_kind
            or (AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM),
            target_type="channel_connection",
            target_id=connection.id,
            target_label=connection.channel.value,
            meta={"channel": connection.channel.value, **(meta or {})},
        )


__all__ = ["LIVE_CONNECTION_INDEX", "ChannelConnectionService"]
