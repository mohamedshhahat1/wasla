"""Data access for WhatsApp accounts and the raw event log."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
            .order_by(WhatsAppAccount.ownership_started_at.desc())
            .limit(MAX_OWNERSHIP_HISTORY)
        )


class InboundEventSweep(BaseRepository[WhatsAppEvent]):
    """The unscoped read over the inbound log, for the recovery sweep only.

    Deliberately not workspace-scoped, and deliberately its own class so that
    is visible. A backlog of unfinished inbound work is a platform-wide
    condition - the Redis outage that produced it did not choose a workspace -
    and a sweeper constructed once per workspace would need a list of every
    workspace to iterate, which is a worse thing to maintain than one honest
    exception to scoping.

    Nothing here is reachable from an API route. The two callers are
    `InboundRecoveryWorker` and the metrics exposition, and both are counting
    or finishing work rather than answering a person.
    """

    model = WhatsAppEvent

    async def claim_unprocessed(
        self,
        *,
        older_than: datetime,
        limit: int,
    ) -> list[WhatsAppEvent]:
        """Events still owing work, locked so one sweeper gets each.

        `FOR UPDATE SKIP LOCKED` rather than a plain read: several sweepers may
        run, and two of them recovering one event would enqueue the same agent
        turn twice - the duplicate customer reply the whole delivery design
        exists to prevent.

        `older_than` keeps the sweep off events that are merely in flight. An
        event stored a second ago has not failed; it is being processed by the
        request that stored it, whose transaction has not committed.

        Compared against `created_at`, not `received_at`: `received_at` is
        Meta's own timestamp, and a late redelivery carries one from days ago.
        Sweeping on that would claim an event the request beside it is still
        holding, which is exactly what the age threshold exists to prevent.
        """
        return await self._all(
            self._select()
            .where(
                WhatsAppEvent.state == WhatsAppEventState.RECEIVED,
                WhatsAppEvent.created_at < older_than,
            )
            .order_by(WhatsAppEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def backlog(self, *, older_than: datetime) -> tuple[int, float]:
        """How many events still owe work, and how old the oldest one is.

        Returned together because they are read together: the count says
        whether there is a backlog and the age says whether it is being
        drained. Two queries would let an operator see a count from one moment
        and an age from another.
        """
        rows = await self.session.execute(
            select(
                func.count(WhatsAppEvent.id),
                func.min(WhatsAppEvent.created_at),
            ).where(
                WhatsAppEvent.state == WhatsAppEventState.RECEIVED,
                WhatsAppEvent.created_at < older_than,
            )
        )
        count, oldest = rows.one()
        if not count or oldest is None:
            return 0, 0.0
        return int(count), max((datetime.now(UTC) - oldest).total_seconds(), 0.0)


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

        The read is the fast path, not the guarantee, and the insert says so:
        `ON CONFLICT DO NOTHING` makes a delivery that loses the race read back
        the winner rather than raise. Before that, two simultaneous deliveries
        of one event both missed the read, both inserted, and the loser turned
        `UNIQUE(tenant_id, event_id)` into a 500 - an internal error for a
        situation that is neither internal nor an error, on an endpoint whose
        failure rate Meta watches (MSG-09). The data was always right; the
        protocol was not.

        The conflict target is named rather than left to the statement, so any
        *other* integrity failure on this insert still raises. A duplicate
        event is a race; anything else is a bug.
        """
        existing = await self.get_by_event_id(event_id)
        if existing is not None:
            return existing, False

        values = {
            "id": uuid.uuid4(),
            "tenant_id": self.tenant_id,
            "account_id": account_id,
            "event_id": event_id,
            "kind": kind,
            "state": WhatsAppEventState.RECEIVED,
            "payload": payload,
            "received_at": received_at,
        }
        statement = (
            pg_insert(WhatsAppEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "event_id"])
            .returning(WhatsAppEvent.id)
        )
        inserted = await self.session.execute(statement)
        if inserted.scalar_one_or_none() is None:
            # Somebody else stored it between the read and the insert. Their
            # row is the canonical one; this delivery is a duplicate and its
            # caller must not project a second time.
            winner = await self.get_by_event_id(event_id)
            if winner is not None:
                return winner, False
            # Unreachable in practice - the conflict proves a row exists - but
            # a concurrent delete would get here, and inventing a row would be
            # worse than saying so.
            raise ConflictError("That WhatsApp event could not be stored.")

        # Read back rather than constructed, so the returned object is the
        # session's mapped row: the caller advances its state, and a detached
        # copy would drop that write on the floor.
        stored = await self.get_by_event_id(event_id)
        if stored is None:  # pragma: no cover - the insert above returned an id
            raise ConflictError("That WhatsApp event could not be stored.")
        return stored, True

    def mark_processed(self, event: WhatsAppEvent) -> WhatsAppEvent:
        """Every handoff this event needed has been made.

        Not "the row was written" - a stored message nobody was ever asked to
        answer is exactly the failure this state exists to make visible
        (MSG-02). `PROCESSED` means the projection landed *and* whatever had to
        be queued was accepted by the queue.
        """
        event.state = WhatsAppEventState.PROCESSED
        event.processed_at = datetime.now(UTC)
        event.error = None
        return event

    def mark_unprocessed(self, event: WhatsAppEvent, *, reason: str) -> WhatsAppEvent:
        """The event is stored and something downstream did not happen.

        Left at `RECEIVED` deliberately rather than moved to `FAILED`: the work
        is still owed, and the sweeper claims exactly this state. The reason is
        a bounded machine-readable token, never a payload fragment or a
        provider message - this column is read by operators and shipped in
        logs.
        """
        event.state = WhatsAppEventState.RECEIVED
        event.error = reason[:500]
        return event

    def mark_failed(self, event: WhatsAppEvent, *, reason: str) -> WhatsAppEvent:
        """The event can never be processed, and no retry will change that.

        Terminal, and the one state the sweeper will not pick up again. Used
        for an event whose ownership cannot be established and for one whose
        payload this database cannot represent - both of which are permanent
        facts about the event rather than transient facts about the system.
        """
        event.state = WhatsAppEventState.FAILED
        event.processed_at = datetime.now(UTC)
        event.error = reason[:500]
        return event
