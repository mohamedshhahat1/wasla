"""Data access for WhatsApp accounts and the raw event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.db.models.channel import Channel
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
)
from app.repositories.base import BaseRepository, TenantScopedRepository

# The inbound log's sweep and retention are channel-neutral; importable from
# here under the names they have always had.
from app.repositories.channel_event_repository import (
    ChannelEventRepository,
    InboundEventSweep,
    WebhookPayloadRetention,
)

logger = get_logger(__name__)

# The partial unique index that makes one live claim per number possible.
# Named here because the conflict handler below has to recognise it by name:
# any *other* integrity failure on this insert is a bug, not a race.
LIVE_NUMBER_INDEX = "uq_whatsapp_accounts_live_phone_number_id"


# Why an event could not be attributed to a workspace. Bounded strings, because
# they are written to `whatsapp_events.error`, counted, and logged - none of
# which may carry a customer number or a message body.
UNKNOWN_NUMBER = "unknown_number"
LATE_UNOWNED = "historical_owner_unknown"
AMBIGUOUS_OWNERSHIP = "ambiguous_ownership"

# How many claims on one number the historical walk will look at. A number that
# has genuinely changed hands fifty times is not a number, it is a data defect,
# and walking further to find out would make one webhook pay for it.
MAX_OWNERSHIP_HISTORY = 50


@dataclass(frozen=True, slots=True)
class OwnershipResolution:
    """Which workspace held a number when an event happened, or why not.

    A pair rather than an optional account, because the three ways to fail are
    operationally different: a number nobody has ever connected is somebody
    else's traffic, a gap between two claims is a message that has no owner,
    and two claims overlapping is a broken invariant somebody has to repair.
    Collapsing them into `None` would make all three look like the first.
    """

    account: WhatsAppAccount | None
    reason: str | None = None

    @classmethod
    def held_by(cls, account: WhatsAppAccount) -> OwnershipResolution:
        return cls(account=account)

    @classmethod
    def unresolved(cls, reason: str) -> OwnershipResolution:
        return cls(account=None, reason=reason)


class WhatsAppAccountDirectory(BaseRepository[WhatsAppAccount]):
    """The one deliberately unscoped lookup in this module.

    Resolving `phone_number_id` is how the workspace is discovered in the first
    place, so it cannot be workspace-scoped. It is isolated here so the
    exception to scoping stays visible in review.
    """

    model = WhatsAppAccount

    async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppAccount | None:
        """The workspace that currently holds this number, if any.

        Released rows are excluded. They still exist - they carry the
        conversation history of a number a workspace used to hold - but they
        must never resolve inbound traffic, or a number handed to a new
        workspace would keep delivering its messages to the old one.

        **This answers "who holds it now", which is the wrong question for an
        arriving event.** It is what a claim attempt checks, and what a caller
        holding a live number wants. Routing asks `owner_at` (ADR-101).
        """
        return await self._first(
            self._select().where(
                WhatsAppAccount.phone_number_id == phone_number_id,
                WhatsAppAccount.released_at.is_(None),
            )
        )

    async def owner_at(self, phone_number_id: str, instant: datetime) -> OwnershipResolution:
        """The workspace that held this number when the event happened.

        Meta retries an undelivered webhook for up to seven days. Resolving a
        number to whoever holds it *now* therefore hands the previous owner's
        customer messages - body, phone number, profile name - to whoever has
        claimed the number since: a disclosure produced by an ordinary product
        operation, with neither workspace doing anything wrong (MSG-01).

        **The property being protected is cross-workspace disclosure, not
        chronology.** That distinction decides the one case a pure interval
        test gets wrong. A provider timestamp can legitimately precede the
        claim it belongs to - clocks drift, and Meta can hold a message sent
        moments before a claim committed - and refusing those would drop real
        customer messages on a number nobody has ever handed over. So an
        instant nobody's tenure covers is attributed to the live claim when
        every claim this number has ever carried belongs to that same
        workspace, because then there is no other workspace it could belong
        to. The moment a second workspace appears in the number's history, that
        reasoning is gone and the event is left unresolved.

        **Never falls back to the current owner across a handover.** A message
        with no establishable owner is dropped by the caller and counted.
        Losing a stray message is bad; handing it to a stranger is worse, and
        unlike losing it, that cannot be undone.
        """
        live = await self.get_by_phone_number_id(phone_number_id)
        if live is not None and live.held_at(instant):
            return OwnershipResolution.held_by(live)

        history = await self.holders_of(phone_number_id)
        if not history:
            return OwnershipResolution.unresolved(UNKNOWN_NUMBER)

        matches = [account for account in history if account.held_at(instant)]
        if len(matches) == 1:
            return OwnershipResolution.held_by(matches[0])
        if matches:
            # Two workspaces holding one number at one instant. The partial
            # unique index makes that impossible among live claims, so reaching
            # here means released rows overlap - a repair job, not a routing
            # decision. Picking one would file a customer message in a
            # workspace chosen by sort order.
            logger.error(
                "whatsapp.ambiguous_number_ownership",
                extra={
                    "event": "whatsapp.ambiguous_number_ownership",
                    "phone_number_id": phone_number_id,
                    "claims": len(matches),
                },
            )
            return OwnershipResolution.unresolved(AMBIGUOUS_OWNERSHIP)

        if live is not None and all(account.tenant_id == live.tenant_id for account in history):
            # One workspace, every claim it has ever made, and an instant just
            # outside them. Nobody else can be owed this message.
            return OwnershipResolution.held_by(live)

        # The number is known and has been held by more than one workspace, and
        # none of them held it then: before the first claim, or in the gap
        # between one workspace releasing it and the next claiming it.
        return OwnershipResolution.unresolved(LATE_UNOWNED)

    async def holders_of(self, phone_number_id: str) -> list[WhatsAppAccount]:
        """Every workspace that has ever claimed this number, newest claim first.

        The candidate set for reconciling a delivery status, whose identity is
        the provider message id rather than the number: a status for a message
        workspace A sent has to find A row even after B has taken the number
        over (MSG-04). Bounded by the same walk limit as `owner_at`, and never
        used to answer a caller query - only to resolve an id Meta supplied.
        """
        return await self._all(
            self._select()
            .where(WhatsAppAccount.phone_number_id == phone_number_id)
            .order_by(WhatsAppAccount.ownership_started_at.desc(), WhatsAppAccount.id.desc())
            .limit(MAX_OWNERSHIP_HISTORY)
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
            .order_by(WhatsAppAccount.created_at.desc(), WhatsAppAccount.id.desc())
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
            # Set here, explicitly, because this is the moment the claim
            # begins and because inbound routing reads it to decide which
            # workspace a late event belongs to (ADR-101). Taken before the
            # insert so a row that loses the race below carries the instant it
            # attempted the claim rather than one the database chose.
            ownership_started_at=datetime.now(UTC),
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


class WhatsAppEventRepository(ChannelEventRepository):
    """The inbound log, under the name and signature WhatsApp callers already use.

    The log is channel-neutral now (`ChannelEventRepository`); this keeps
    `record(account_id=...) -> (event, created)` for the callers written
    against it. A WhatsApp event id is a Meta message id and cannot collide
    across numbers, so a collision here is a broken invariant and raises.
    """

    async def record(
        self,
        *,
        account_id: uuid.UUID,
        event_id: str,
        kind: WhatsAppEventKind,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> tuple[WhatsAppEvent, bool]:
        stored = await self.store(
            connection_id=account_id,
            channel=Channel.WHATSAPP,
            event_id=event_id,
            kind=kind,
            payload=payload,
            received_at=received_at,
        )
        if stored.event is None:
            raise ConflictError("That WhatsApp event could not be stored.")
        return stored.event, stored.created


__all__ = [
    "AMBIGUOUS_OWNERSHIP",
    "LATE_UNOWNED",
    "LIVE_NUMBER_INDEX",
    "UNKNOWN_NUMBER",
    "InboundEventSweep",
    "OwnershipResolution",
    "WebhookPayloadRetention",
    "WhatsAppAccountDirectory",
    "WhatsAppAccountRepository",
    "WhatsAppEventRepository",
]
