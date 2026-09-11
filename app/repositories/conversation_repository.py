"""Data access for contacts, conversations and messages."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.core.pagination import Cursor
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.sentiment import ConversationPriority
from app.repositories.base import BaseRepository, TenantScopedRepository

# The constraint the idempotency claim races on. Named so the conflict handler
# can recognise it: any *other* integrity failure on that insert is a bug.
IDEMPOTENCY_CONSTRAINT = "uq_messages_tenant_id_idempotency_key"

# The two identity constraints inbound projection races on. Named so each
# conflict handler recognises its own: any other integrity failure on those
# inserts is a bug rather than a burst of traffic.
CONTACT_IDENTITY_CONSTRAINT = "uq_contacts_tenant_id_wa_id"
CONVERSATION_IDENTITY_CONSTRAINT = "uq_conversations_tenant_id_contact_id_account_id"


def _after_nullable(model: type[Conversation], after: Cursor) -> ColumnElement[bool]:
    """Rows following `after` under `ORDER BY last_message_at DESC NULLS LAST, id DESC`.

    Two cases, because a null sort value is not comparable and SQL will not do
    this for us. With a timestamp in hand, what follows is anything older, the
    same instant with a smaller id, or the whole null block that sorts after
    every timestamp. Once the cursor is itself null the reader is already inside
    that block, and only a smaller id follows.
    """
    column = model.last_message_at
    if after.sort_value is None:
        return and_(column.is_(None), model.id < after.id)
    return or_(
        column < after.sort_value,
        and_(column == after.sort_value, model.id < after.id),
        column.is_(None),
    )


class OutboundMessageDirectory(BaseRepository[Message]):
    """The one unscoped message lookup, and the only caller is the webhook.

    A delivery status identifies itself by the provider message id, not by the
    number it arrived on - so after a number changes hands, resolving the
    workspace from the number and *then* looking the message up inside it finds
    nothing, and every message the previous workspace had in flight stays
    unreconciled for ever (MSG-04).

    Unscoped, and therefore fenced three ways. The candidate workspaces are
    supplied by the caller and come from `WhatsAppAccountDirectory.holders_of`,
    so they are exactly the workspaces that have held the number Meta is
    reporting about. An empty candidate set returns nothing rather than
    searching the platform. And no API route reaches this class: it is
    constructed by the ingestion service and by the inbound sweeper, both of
    which are answering Meta rather than a person.
    """

    model = Message

    async def find_by_wa_message_id(
        self,
        wa_message_id: str,
        *,
        tenant_ids: Sequence[uuid.UUID],
    ) -> Message | None:
        """The outbound message this provider id names, among these workspaces.

        `UNIQUE(tenant_id, wa_message_id)` makes at most one row match per
        workspace, and a provider id belongs to one workspace because a number
        resolves to one live claim at a time - so this is a single row or none.
        """
        if not tenant_ids:
            return None
        return await self._first(
            self._select().where(
                Message.wa_message_id == wa_message_id,
                Message.tenant_id.in_(tenant_ids),
                Message.direction == MessageDirection.OUTBOUND,
            )
        )


class UnresolvedOutboundDirectory(BaseRepository[Message]):
    """Sends whose outcome nobody knows, across the whole deployment.

    `docs/WHATSAPP.md` said a `requested` row "is shown to a person rather than
    resolved by a sweep", and named `ix_messages_unresolved_delivery` as the
    query that finds them. The index existed and was correctly partial. Nothing
    read it - no route, no command, no metric, no runbook section - so the rows
    accumulated invisibly and nobody could answer "did this go out?" without
    SQL nobody had been given (MSG-11).

    Deliberately *not* a resolver. `REQUESTED` means Meta may already have
    delivered the message, and the whole reason it is terminal is that sending
    it again is the one action that cannot be taken back. This class exists so
    a person can find those rows and read the conversation, which is the only
    thing that can actually settle them.

    Unscoped for the same reason the inbound sweep is: a backlog of unresolved
    sends is a platform-wide condition, and the two callers - the metrics
    exposition and the operator command - are counting rather than answering a
    person.
    """

    model = Message

    async def backlog(self) -> tuple[int, float]:
        """How many sends are unresolved, and how old the oldest one is.

        Read together because they are alerted on together: the count says
        whether anything is outstanding and the age says whether it is
        outstanding because a send is in flight right now or because one broke
        an hour ago and nobody noticed.

        Scoped to `REQUESTED` alone. `CLAIMED` is the other half of the partial
        index and means the opposite thing - provably nothing was delivered -
        so counting it here would inflate a number whose entire meaning is "may
        be on somebody's phone".
        """
        rows = await self.session.execute(
            select(func.count(Message.id), func.min(Message.created_at)).where(
                Message.delivery_state == MessageDeliveryState.REQUESTED
            )
        )
        count, oldest = rows.one()
        if not count or oldest is None:
            return 0, 0.0
        return int(count), max((datetime.now(UTC) - oldest).total_seconds(), 0.0)

    async def list_unresolved(self, *, limit: int) -> list[Message]:
        """The oldest unresolved sends, for an operator to work through.

        Oldest first, because age is what makes one of these worth
        investigating: a row a few seconds old is a send in flight.
        """
        return await self._all(
            self._select()
            .where(Message.delivery_state == MessageDeliveryState.REQUESTED)
            .order_by(Message.created_at)
            .limit(limit)
        )


class ContactRepository(TenantScopedRepository[Contact]):
    """Customers of one workspace."""

    model = Contact

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Contact.tenant_id == self.tenant_id

    async def get_by_wa_id(self, wa_id: str) -> Contact | None:
        return await self._first(self._select().where(Contact.wa_id == wa_id))

    async def get_by_id(self, contact_id: uuid.UUID) -> Contact | None:
        return await self._first(self._select().where(Contact.id == contact_id))

    async def require_by_id(self, contact_id: uuid.UUID) -> Contact:
        return await self._require(self._select().where(Contact.id == contact_id))

    async def upsert(
        self,
        *,
        wa_id: str,
        display_name: str | None = None,
        last_seen_at: datetime | None = None,
    ) -> Contact:
        """Find or create the contact, refreshing what Meta told us.

        Meta sends the profile name with inbound traffic and customers change
        it, so the stored name is refreshed when a newer one arrives. An absent
        name never erases a known one.

        The read is the fast path; `UNIQUE(tenant_id, wa_id)` is the guarantee.
        A customer's first two messages can arrive in one burst, and before
        this handler both deliveries missed the read, both inserted, and the
        loser's `IntegrityError` became a 500 (MSG-09). The savepoint is what
        makes the loss recoverable: without it the failed insert poisons the
        surrounding transaction and the request cannot even answer.
        """
        contact = await self.get_by_wa_id(wa_id)
        if contact is None:
            contact = await self._insert_contact(
                wa_id=wa_id,
                display_name=display_name,
                last_seen_at=last_seen_at,
            )
            if contact is not None:
                return contact
            # Somebody else created it between the read and the insert. Their
            # row is the one that exists, so fall through and refresh it as if
            # the read had found it - which for this customer it now has.
            contact = await self.get_by_wa_id(wa_id)
            if contact is None:  # pragma: no cover - the conflict proves a row
                raise ConflictError("That contact could not be stored.")

        if display_name is not None:
            contact.display_name = display_name
        if last_seen_at is not None and (
            contact.last_seen_at is None or last_seen_at > contact.last_seen_at
        ):
            contact.last_seen_at = last_seen_at
        return contact

    async def _insert_contact(
        self,
        *,
        wa_id: str,
        display_name: str | None,
        last_seen_at: datetime | None,
    ) -> Contact | None:
        """Insert, or None if another delivery got there first.

        The savepoint scopes the failure to this statement. Any integrity
        failure that is not the identity constraint is re-raised, because a
        duplicate `wa_id` is a race and anything else here is a bug.
        """
        contact = Contact(
            tenant_id=self.tenant_id,
            wa_id=wa_id,
            display_name=display_name,
            last_seen_at=last_seen_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(contact)
                await self.session.flush()
        except IntegrityError as error:
            if CONTACT_IDENTITY_CONSTRAINT not in str(error.orig):
                raise
            return None
        return contact


class ConversationRepository(TenantScopedRepository[Conversation]):
    """Conversations of one workspace."""

    model = Conversation

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Conversation.tenant_id == self.tenant_id

    async def get_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._first(self._select().where(Conversation.id == conversation_id))

    async def require_by_id(self, conversation_id: uuid.UUID) -> Conversation:
        return await self._require(self._select().where(Conversation.id == conversation_id))

    async def get_for_contact(
        self,
        *,
        contact_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Conversation | None:
        return await self._first(
            self._select().where(
                Conversation.contact_id == contact_id,
                Conversation.account_id == account_id,
            )
        )

    async def get_or_create(
        self,
        *,
        contact_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> tuple[Conversation, bool]:
        """Returns the conversation and whether it was created.

        As with event storage, the read is the fast path and
        `UNIQUE(tenant_id, contact_id, account_id)` is the guarantee - and the
        guarantee now answers rather than raising. Two of a customer's first
        messages arriving together both missed the read and both inserted, and
        the loser turned the constraint into a 500 (MSG-09). The invariants
        were never in doubt; the response was.

        The savepoint keeps the failed insert from poisoning the surrounding
        transaction, which is what would otherwise leave the request unable to
        produce any answer at all. The same shape
        `WhatsAppAccountRepository.connect` uses for the number-claim race.
        """
        existing = await self.get_for_contact(contact_id=contact_id, account_id=account_id)
        if existing is not None:
            return existing, False

        conversation = Conversation(
            tenant_id=self.tenant_id,
            contact_id=contact_id,
            account_id=account_id,
            status=ConversationStatus.OPEN,
            mode=ConversationMode.AI,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(conversation)
                await self.session.flush()
        except IntegrityError as error:
            if CONVERSATION_IDENTITY_CONSTRAINT not in str(error.orig):
                raise
            winner = await self.get_for_contact(contact_id=contact_id, account_id=account_id)
            if winner is None:  # pragma: no cover - the conflict proves a row
                raise
            return winner, False
        return conversation, True

    async def list_open(
        self,
        *,
        limit: int = 50,
        after: Cursor | None = None,
        priority: ConversationPriority | None = None,
    ) -> list[Conversation]:
        """Everything not closed, most recently active first.

        Ordered by `last_message_at` with nulls last and `id` as the
        tiebreaker. A conversation that has never carried a message sorts to the
        end rather than the front, which is what PostgreSQL would otherwise do
        with a descending sort.

        `priority` filters rather than reorders. Sorting the whole inbox by
        urgency would bury the ordinary queue under whatever the classifier
        flagged today; a filter lets somebody work the flagged ones deliberately
        and leaves the default view alone.
        """
        query = (
            self._select()
            .where(Conversation.status != ConversationStatus.CLOSED)
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.id.desc(),
            )
            .limit(limit)
        )
        if priority is not None:
            query = query.where(Conversation.priority == priority)
        if after is not None:
            query = query.where(_after_nullable(Conversation, after))
        return await self._all(query)

    async def touch_inbound(self, conversation: Conversation, *, at: datetime) -> None:
        """Record customer activity, which reopens the service window.

        A closed conversation reopens: a customer writing again is a live
        conversation whatever an agent previously decided.
        """
        conversation.last_inbound_at = at
        conversation.last_message_at = at
        if conversation.status is ConversationStatus.CLOSED:
            conversation.status = ConversationStatus.OPEN


class MessageRepository(TenantScopedRepository[Message]):
    """Messages of one workspace."""

    model = Message

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Message.tenant_id == self.tenant_id

    async def get_by_wa_message_id(self, wa_message_id: str) -> Message | None:
        return await self._first(self._select().where(Message.wa_message_id == wa_message_id))

    async def list_for_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[Message]:
        """Most recent messages first.

        `created_at` is never null here, so the keyset is a plain row
        comparison - no nulls-last branch is needed.
        """
        query = (
            self._select()
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        if after is not None and after.sort_value is not None:
            query = query.where(
                or_(
                    Message.created_at < after.sort_value,
                    and_(
                        Message.created_at == after.sort_value,
                        Message.id < after.id,
                    ),
                )
            )
        return await self._all(query)

    async def latest_inbound(self, conversation_id: uuid.UUID) -> Message | None:
        """The most recent thing the customer said.

        What sentiment analysis reads. Only the newest one: a job that waited
        behind others is answering the conversation as it stands now, and the
        mood that matters is the mood of the message that prompted it.

        "Newest" is by `created_at`, and Meta can deliver several messages in
        one webhook. Those are stored in one transaction, so they share a
        timestamp - PostgreSQL's `now()` is the transaction's start - and which
        of them this returns is then decided by the id tie-break rather than by
        arrival order. Accepted: messages that arrived together came from the
        same moment and carry the same mood, and the alternative is a monotonic
        column on the busiest table in the schema.
        """
        return await self._first(
            self._select()
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
        )

    async def record_inbound(
        self,
        *,
        conversation_id: uuid.UUID,
        wa_message_id: str,
        kind: MessageKind,
        body: str | None,
        sent_at: datetime,
    ) -> tuple[Message, bool]:
        """Store a customer message once. Returns the row and whether it is new."""
        existing = await self.get_by_wa_message_id(wa_message_id)
        if existing is not None:
            return existing, False

        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            wa_message_id=wa_message_id,
            direction=MessageDirection.INBOUND,
            kind=kind,
            status=MessageStatus.RECEIVED,
            body=body,
            sent_at=sent_at,
        )
        return self.add(message), True

    async def stage_outbound(
        self,
        *,
        conversation_id: uuid.UUID,
        kind: MessageKind,
        body: str | None,
        sent_by_id: uuid.UUID | None = None,
        template_name: str | None = None,
        template_language: str | None = None,
        idempotency_key: str | None = None,
    ) -> Message:
        """Create the row before calling Meta.

        Written first and deliberately without a `wa_message_id`, so a send that
        never completes still leaves evidence it was attempted rather than
        vanishing.

        `CLAIMED` rather than nothing: this row is committed before Meta is
        asked for anything, and the state is what says so. A caller reading it
        knows the message has not been delivered and cannot have been
        (ADR-093).

        The idempotency key is written *here*, in the same statement that
        claims the send, which is what makes it a claim rather than a note. A
        second request carrying the same key loses at this insert and reads the
        first request's row back rather than asking Meta again (MSG-15).
        """
        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=kind,
            status=MessageStatus.PENDING,
            delivery_state=MessageDeliveryState.CLAIMED,
            body=body,
            sent_by_id=sent_by_id,
            template_name=template_name,
            template_language=template_language,
            idempotency_key=idempotency_key,
        )
        return self.add(message)

    async def get_by_idempotency_key(self, key: str) -> Message | None:
        """The message a previous request with this key produced, if any."""
        return await self._first(self._select().where(Message.idempotency_key == key))

    async def claim_idempotency_key(
        self,
        *,
        conversation_id: uuid.UUID,
        kind: MessageKind,
        body: str | None,
        sent_by_id: uuid.UUID | None,
        template_name: str | None,
        template_language: str | None,
        idempotency_key: str,
    ) -> tuple[Message, bool]:
        """Stage a send under this key, or hand back the one already staged.

        Returns the message and whether this call created it.

        The savepoint is the mechanism, and it is the same one
        `WhatsAppAccountRepository.connect` uses for the number-claim race. Two
        simultaneous submissions of one key both miss the read and both insert;
        the loser's `IntegrityError` would otherwise poison the surrounding
        transaction, leaving the request unable even to produce a response
        body. Wrapped in a savepoint, only the failed insert unwinds and the
        caller can re-read the row that won.

        Any integrity failure that is *not* this constraint is re-raised. A
        duplicate key is a race; anything else on this insert is a bug.
        """
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing, False

        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=kind,
            status=MessageStatus.PENDING,
            delivery_state=MessageDeliveryState.CLAIMED,
            body=body,
            sent_by_id=sent_by_id,
            template_name=template_name,
            template_language=template_language,
            idempotency_key=idempotency_key,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(message)
                await self.session.flush()
        except IntegrityError as error:
            if IDEMPOTENCY_CONSTRAINT not in str(error.orig):
                raise
            winner = await self.get_by_idempotency_key(idempotency_key)
            if winner is None:  # pragma: no cover - the conflict proves a row
                raise
            return winner, False
        return message, True

    async def mark_sent(
        self,
        message: Message,
        *,
        wa_message_id: str,
        sent_at: datetime,
    ) -> Message:
        message.wa_message_id = wa_message_id
        message.status = MessageStatus.SENT
        message.sent_at = sent_at
        message.delivery_state = MessageDeliveryState.SENT
        return message

    async def mark_failed(self, message: Message, *, reason: str) -> Message:
        """Record a send that provably delivered nothing.

        Only reached where that is known - Meta declined the request, or the
        request never left. A send whose outcome is unknown is left in
        `REQUESTED`, because `FAILED` is a claim about the customer's phone
        that nobody is in a position to make.
        """
        message.status = MessageStatus.FAILED
        message.failure_reason = reason[:500]
        message.delivery_state = MessageDeliveryState.UNDELIVERED
        return message

    async def apply_status(
        self,
        *,
        wa_message_id: str,
        status: MessageStatus,
        at: datetime,
    ) -> Message | None:
        """Project a delivery status onto its message, found in this workspace.

        Returns None when the message is unknown, which is normal for traffic
        sent outside Wasla. The ingestion path resolves the message itself,
        across every workspace that has held the number, and calls
        `advance_status` with what it found (MSG-04); this remains the lookup
        for callers holding only an id and a workspace.
        """
        message = await self.get_by_wa_message_id(wa_message_id)
        if message is None:
            return None
        return self.advance_status(message, status=status, at=at)

    def advance_status(
        self,
        message: Message,
        *,
        status: MessageStatus,
        at: datetime,
    ) -> Message:
        """Move a message forward, and never backwards.

        Statuses arrive out of order and are redelivered, so every timestamp is
        written once and the visible status only ever climbs.

        **Delivery evidence outranks a failure report, and that is the ordering
        below.** `failed` used to be set unconditionally and to win from any
        state, which made two contradictions reachable: a message the customer
        demonstrably read could be downgraded to `failed`, and a row could
        carry `failed` beside a non-null `delivered_at` - a pair a reader has
        to pick a side on, and the projection had already picked the wrong one
        (MSG-14). Ranking `failed` above `sent` and below `delivered` settles
        both: a send that never arrived still fails, and a message Meta has
        confirmed arriving is not un-delivered by a later report.

        The report is not discarded either way. `failure_reason` records that
        Meta said the message failed, so a contradiction is visible on the row
        rather than only in a log nobody is reading.
        """
        if status is MessageStatus.DELIVERED and message.delivered_at is None:
            message.delivered_at = at
        elif status is MessageStatus.READ and message.read_at is None:
            message.read_at = at
        elif status is MessageStatus.FAILED and message.failure_reason is None:
            message.failure_reason = PROVIDER_REPORTED_FAILURE

        if _STATUS_ORDER[status] > _STATUS_ORDER[message.status]:
            message.status = status
        return message


# What a provider `failed` status records when it cannot claim the message.
# A fixed sentence rather than Meta's own error text: that text can echo
# fragments of the request, and this column is read back by an API.
PROVIDER_REPORTED_FAILURE = "WhatsApp reported this message as failed."

# Only the outbound progression is ordered; the rest share the floor so an
# unexpected status can never appear to advance a message.
#
# `FAILED` sits between `SENT` and `DELIVERED` deliberately. It has to beat
# `sent`, which is only an acknowledgement that Meta accepted the message, and
# it must not beat `delivered` or `read`, which are reports that it arrived.
_STATUS_ORDER: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.PENDING: 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
}
