"""Data access for WhatsApp accounts and the raw event log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = get_logger(__name__)

# The partial unique index that makes one live claim per number possible.
# Named here because the conflict handler below has to recognise it by name:
# any *other* integrity failure on this insert is a bug, not a race.
LIVE_NUMBER_INDEX = "uq_whatsapp_accounts_live_phone_number_id"


class WhatsAppAccountDirectory(BaseRepository[WhatsAppAccount]):
    """The one deliberately unscoped lookup in this module.

    Resolving `phone_number_id` is how the workspace is discovered in the first
    place, so it cannot be workspace-scoped. It is isolated here, holding two
    methods, so the exception to scoping stays visible in review.
    """

    model = WhatsAppAccount

    async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppAccount | None:
        """The workspace that currently holds this number, if any.

        Released rows are excluded. They still exist - they carry the
        conversation history of a number a workspace used to hold - but they
        must never resolve inbound traffic, or a number handed to a new
        workspace would keep delivering its messages to the old one.
        """
        return await self._first(
            self._select().where(
                WhatsAppAccount.phone_number_id == phone_number_id,
                WhatsAppAccount.released_at.is_(None),
            )
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

    async def require_live_by_id(self, account_id: uuid.UUID) -> WhatsAppAccount:
        """An account this workspace still holds.

        A released row is not found. Enabling or releasing one again would be a
        no-op at best and, if the number has since been claimed by somebody
        else, a claim on their traffic at worst.
        """
        return await self._require(
            self._select().where(
                WhatsAppAccount.id == account_id,
                WhatsAppAccount.released_at.is_(None),
            )
        )

    async def list_all(self, *, limit: int = 50) -> list[WhatsAppAccount]:
        return await self._all(
            self._select()
            .where(WhatsAppAccount.released_at.is_(None))
            .order_by(WhatsAppAccount.created_at.desc())
            .limit(limit)
        )

    async def connect(
        self,
        *,
        phone_number_id: str,
        waba_id: str,
        display_phone_number: str,
        display_name: str | None = None,
        verified_name: str | None = None,
        ownership_verified_at: datetime | None = None,
    ) -> WhatsAppAccount:
        """Claim a phone number for this workspace.

        Two claims on the same number, and why both checks are here:

        The read is the fast path. It gives a clean 409 in the ordinary case -
        somebody typing in a number their colleague connected last week - and it
        can say so without a failed insert.

        The **index** is the guarantee. Two requests arriving together both miss
        the read, both insert, and one of them loses at flush. That loss arrives
        as an `IntegrityError`, which without this handler would surface as a
        500: an internal error for a situation that is neither internal nor an
        error. It is translated here, on the *same* index the read was checking,
        so the two racing callers get the same answer in either order - one 201,
        one 409.

        The insert is wrapped in a savepoint so the failure does not poison the
        surrounding transaction. Without it the request could not go on to
        produce a response body at all.
        """
        directory = WhatsAppAccountDirectory(self.session)
        if await directory.get_by_phone_number_id(phone_number_id) is not None:
            # The uniqueness is platform-wide, so the conflict may be with a
            # number already claimed by a workspace the caller cannot see. The
            # message says only that the number is in use: which workspace holds
            # it is not the caller's business.
            raise ConflictError("That WhatsApp number is already connected.")

        account = WhatsAppAccount(
            tenant_id=self.tenant_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            display_phone_number=display_phone_number,
            display_name=display_name,
            verified_name=verified_name,
            ownership_verified_at=ownership_verified_at,
            status=WhatsAppAccountStatus.ACTIVE,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(account)
                await self.session.flush()
        except IntegrityError as error:
            if LIVE_NUMBER_INDEX not in str(error.orig):
                raise
            logger.warning(
                "whatsapp.concurrent_claim_rejected",
                extra={
                    "event": "whatsapp.concurrent_claim_rejected",
                    "phone_number_id": phone_number_id,
                },
            )
            raise ConflictError("That WhatsApp number is already connected.") from error
        return account


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
