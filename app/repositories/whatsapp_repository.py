"""Data access for WhatsApp accounts and the raw event log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement

from app.core.exceptions import ConflictError
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


class WhatsAppAccountDirectory(BaseRepository[WhatsAppAccount]):
    """The one deliberately unscoped lookup in this module.

    Resolving `phone_number_id` is how the workspace is discovered in the first
    place, so it cannot be workspace-scoped. It is isolated here, holding a
    single method, so the exception to scoping stays visible in review.
    """

    model = WhatsAppAccount

    async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppAccount | None:
        return await self._first(
            self._select().where(WhatsAppAccount.phone_number_id == phone_number_id)
        )


class WhatsAppAccountRepository(TenantScopedRepository[WhatsAppAccount]):
    """Accounts belonging to one workspace."""

    model = WhatsAppAccount

    def _tenant_filter(self) -> ColumnElement[bool]:
        return WhatsAppAccount.tenant_id == self.tenant_id

    async def get_by_id(self, account_id: uuid.UUID) -> WhatsAppAccount | None:
        return await self._first(self._select().where(WhatsAppAccount.id == account_id))

    async def require_by_id(self, account_id: uuid.UUID) -> WhatsAppAccount:
        return await self._require(self._select().where(WhatsAppAccount.id == account_id))

    async def list_all(self, *, limit: int = 50) -> list[WhatsAppAccount]:
        return await self._all(
            self._select().order_by(WhatsAppAccount.created_at.desc()).limit(limit)
        )

    async def connect(
        self,
        *,
        phone_number_id: str,
        waba_id: str,
        display_phone_number: str,
        display_name: str | None = None,
    ) -> WhatsAppAccount:
        """Claim a phone number for this workspace.

        The uniqueness of `phone_number_id` is platform-wide, so the conflict
        may be with a number already claimed by a workspace the caller cannot
        see. The message says only that the number is in use: which workspace
        holds it is not the caller's business.
        """
        directory = WhatsAppAccountDirectory(self.session)
        if await directory.get_by_phone_number_id(phone_number_id) is not None:
            raise ConflictError("That WhatsApp number is already connected.")

        account = WhatsAppAccount(
            tenant_id=self.tenant_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            display_phone_number=display_phone_number,
            display_name=display_name,
            status=WhatsAppAccountStatus.ACTIVE,
        )
        return self.add(account)


class WhatsAppEventRepository(TenantScopedRepository[WhatsAppEvent]):
    """The append-only inbound log for one workspace."""

    model = WhatsAppEvent

    def _tenant_filter(self) -> ColumnElement[bool]:
        return WhatsAppEvent.tenant_id == self.tenant_id

    async def get_by_event_id(self, event_id: str) -> WhatsAppEvent | None:
        return await self._first(self._select().where(WhatsAppEvent.event_id == event_id))

    async def list_recent(self, *, limit: int = 50) -> list[WhatsAppEvent]:
        return await self._all(
            self._select().order_by(WhatsAppEvent.received_at.desc()).limit(limit)
        )

    async def record(
        self,
        *,
        account_id: uuid.UUID,
        event_id: str,
        kind: WhatsAppEventKind,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> tuple[WhatsAppEvent, bool]:
        """Store an event once. Returns the row and whether it is new.

        The read is the fast path, not the guarantee: two simultaneous
        deliveries of the same event will both miss it and the unique
        constraint will reject the loser. Meta retries, and the retry finds the
        row. That is why the constraint exists rather than trusting this check.
        """
        existing = await self.get_by_event_id(event_id)
        if existing is not None:
            return existing, False

        event = WhatsAppEvent(
            tenant_id=self.tenant_id,
            account_id=account_id,
            event_id=event_id,
            kind=kind,
            state=WhatsAppEventState.RECEIVED,
            payload=payload,
            received_at=received_at,
        )
        return self.add(event), True
