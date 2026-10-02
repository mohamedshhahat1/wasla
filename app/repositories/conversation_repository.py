"""Data access for contacts, conversations and messages."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value

from app.channels.inbound import ReplyAction
from app.core.exceptions import ConflictError
from app.core.pagination import Cursor
from app.db.models.channel import Channel, ChannelConnection
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageKind,
    MessageOrigin,
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

    Unscoped, and therefore fenced three ways. The candidate claims are
    supplied by the caller and come from `ConnectionDirectory.holders_of`, so
    they are exactly the claims that have held the connection the provider is
    reporting about. An empty candidate set returns nothing rather than
    searching the platform. And no API route reaches this class: it is
    constructed by the ingestion service, which is answering a provider rather
    than a person.
    """

    model = Message

    async def find_by_provider_message_id(
        self,
        provider_message_id: str,
        *,
        holders: Sequence[ChannelConnection],
    ) -> Message | None:
        """The outbound message a status names, among the messages of these claims.

        A provider id is unique per connection (ADR-120), so the candidates are
        the claims the connection the status arrived on has carried - across
        workspaces (MSG-04) - and a message on any other connection cannot
        match, whoever owns it.

        **The workspaces are in the predicate as well as the connections**
        (OMNI-029). Every status webhook takes this path - WhatsApp reports
        sent, delivered and read separately, three per message - and with only
        `connection_id IN (...)` no index served it: the plan was a parallel
        sequential scan of `messages`, whose cost grew with the whole
        platform's traffic. Each claim belongs to one workspace, so naming the
        claims' workspaces changes no answer and lets
        `uq_messages_tenant_id_connection_id_wa_message_id` serve an index seek
        (`tests/integration/test_status_lookup_plan.py`).
        """
        if not holders:
            return None
        return await self._first(self.provider_message_lookup(provider_message_id, holders=holders))

    @staticmethod
    def provider_message_lookup(
        provider_message_id: str, *, holders: Sequence[ChannelConnection]
    ) -> Select[tuple[Message]]:
        """The statement the lookup issues - built here so its plan can be tested as issued."""
        return select(Message).where(
            Message.tenant_id.in_(sorted({holder.tenant_id for holder in holders})),
            Message.connection_id.in_([holder.id for holder in holders]),
            Message.wa_message_id == provider_message_id,
            Message.direction == MessageDirection.OUTBOUND,
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
            .order_by(Message.created_at, Message.id)
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

    async def require_by_id(
        self,
        conversation_id: uuid.UUID,
        *,
        populate_existing: bool = False,
    ) -> Conversation:
        """This workspace's conversation, or a tenant-scoped miss.

        `populate_existing` overwrites what the identity map already holds with
        what the database holds now. It exists because `expire_on_commit=False`
        makes an ordinary select return the instance as it was *loaded*, and an
        agent turn reads this row again after an inference precisely because it
        may have changed - a colleague taking the conversation over is the case
        that matters (TOOL-21). Off by default: every other caller loads it
        once inside one unit of work and wants the identity map's copy.
        """
        statement = self._select().where(Conversation.id == conversation_id)
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        return await self._require(statement)

    async def lock_by_id(self, conversation_id: uuid.UUID, *, share: bool = False) -> Conversation:
        """This workspace's conversation as committed now, locked until the transaction ends.

        The primitive every ownership transition stands on (CRM-02/03/04). A
        transition - taking a conversation over, releasing it, assigning it -
        is only correct against the row as it is *at the write*, so the row is
        locked, re-read with `populate_existing` (the identity map may hold a
        copy loaded before an inference or before somebody else committed),
        and only then judged. Whoever locks second waits, then reads what the
        first committed, and is judged against that.

        `share` takes `FOR SHARE`: enough to stop the row changing under a
        reader that is about to act on it - the follow-up sweep deciding
        whether a conversation is still the AI's - without queueing behind
        other readers.

        Callers hold it only across database work. Every provider call in this
        codebase goes through `released`, which commits, so no lock taken here
        can survive into a request to OpenAI or Meta.
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        statement = (
            self._select()
            .where(Conversation.id == conversation_id)
            # `FOR NO KEY UPDATE`, not `FOR UPDATE`: every insert that names
            # this conversation - a message, a tool execution, a follow-up -
            # takes `FOR KEY SHARE` on it through its foreign key, and a plain
            # `FOR UPDATE` would queue a takeover behind every one of them.
            # Nothing here changes a key. `share` is a plain `FOR SHARE`, which
            # does conflict with a transition's lock, and that is its purpose.
            .with_for_update(read=share, key_share=not share)
            .execution_options(populate_existing=True)
        )
        return await self._require(statement)

    async def lock_assigned_to(self, user_id: uuid.UUID) -> list[Conversation]:
        """Every conversation here assigned to `user_id`, locked, as committed now.

        For a member's removal, which unassigns them (PD-CRM-2).
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        return await self._all(
            self._select()
            .where(Conversation.assigned_to_id == user_id)
            .order_by(Conversation.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

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
        channel: Channel | None = None,
        participant_identity_id: uuid.UUID | None = None,
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
        # Named by the caller that knows them - the ingestion service, from the
        # connection and the identity that wrote (OMNI-004). A caller naming
        # neither gets the channel default and the trigger's single-identity
        # pin, or a refusal - never a guessed address.
        if channel is not None:
            conversation.channel = channel
        if participant_identity_id is not None:
            conversation.participant_identity_id = participant_identity_id
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
        channel: Channel | None = None,
        connection_id: uuid.UUID | None = None,
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
        # Filters over the one unified inbox, like `priority` (OMNI-014). A
        # connection id from another workspace matches nothing: the tenant
        # predicate is already on the query.
        if channel is not None:
            query = query.where(Conversation.channel == channel)
        if connection_id is not None:
            query = query.where(Conversation.account_id == connection_id)
        if after is not None:
            query = query.where(_after_nullable(Conversation, after))
        return await self._all(query)

    async def touch_inbound(self, conversation: Conversation, *, at: datetime) -> None:
        """Record customer activity, which reopens the service window.

        A closed conversation reopens: a customer writing again is a live
        conversation whatever an agent previously decided.

        **The anchors only ever move forward** (OMNI-036). `at` is the
        provider's timestamp, and providers retry and reorder: Meta retries a
        refused delivery for up to seven days. Assigning it unconditionally let
        a late, older message move `last_inbound_at` backwards - closing a
        service window the provider still held open, so every free-text reply
        was refused until the customer wrote again - and moved the inbox order
        back with it. `GREATEST` in one statement, not a read-modify-write, so
        two concurrent deliveries cannot regress it either.
        """
        await self._advance(conversation, at=at, inbound=True)

    async def disclosure_marks(
        self, conversation_id: uuid.UUID
    ) -> tuple[datetime | None, datetime | None]:
        """When automation was last disclosed, and when the AI last took over again (OMNI-041).

        Columns read now, not the attributes of an object loaded before a long
        inference: a colleague handing the conversation back mid-turn is what
        this must see.
        """
        row = (
            await self.session.execute(
                select(Conversation.automation_disclosed_at, Conversation.ai_resumed_at).where(
                    self._tenant_filter(), Conversation.id == conversation_id
                )
            )
        ).one_or_none()
        if row is None:
            return None, None
        return row[0], row[1]

    async def record_disclosure(self, conversation_id: uuid.UUID, *, at: datetime) -> None:
        """Record a delivered disclosure. Forward only, so a late write never moves it back."""
        await self.session.execute(
            update(Conversation)
            .where(self._tenant_filter(), Conversation.id == conversation_id)
            .values(automation_disclosed_at=func.greatest(Conversation.automation_disclosed_at, at))
            .execution_options(synchronize_session=False)
        )

    async def touch_outbound(self, conversation: Conversation, *, at: datetime) -> None:
        """Record that the business sent something: the inbox order, never the window."""
        await self._advance(conversation, at=at, inbound=False)

    async def _advance(self, conversation: Conversation, *, at: datetime, inbound: bool) -> None:
        values: dict[str, Any] = {
            "last_message_at": func.greatest(Conversation.last_message_at, at),
        }
        returning: list[Any] = [Conversation.last_message_at]
        if inbound:
            values["last_inbound_at"] = func.greatest(Conversation.last_inbound_at, at)
            values["status"] = case(
                (Conversation.status == ConversationStatus.CLOSED, ConversationStatus.OPEN),
                else_=Conversation.status,
            )
            returning += [Conversation.last_inbound_at, Conversation.status]
        row = (
            await self.session.execute(
                update(Conversation)
                .where(self._tenant_filter(), Conversation.id == conversation.id)
                .values(**values)
                .returning(*returning)
                .execution_options(synchronize_session=False)
            )
        ).one()
        # The row's own answer, written back without marking the object dirty,
        # so a later flush cannot write a stale in-memory value over it.
        set_committed_value(conversation, "last_message_at", row[0])
        if inbound:
            set_committed_value(conversation, "last_inbound_at", row[1])
            set_committed_value(conversation, "status", row[2])


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
        """Most recent messages first, in the conversation's own order.

        Ordered by `sequence`, the position the database assigned at insert,
        and never by `created_at`: that is the transaction's start, so every
        message one webhook delivery wrote shares it, and a transcript sorted on
        it is scrambled in a way no log shows (AI-01). This is the one ordering
        the agent's window and the inbox both read.

        The cursor's id names the last message of the previous page, and its
        position is read here rather than carried in the cursor - so the cursor
        format callers already hold is unchanged, and a cursor naming a message
        in another conversation or workspace resolves to no position and
        matches nothing.
        """
        query = (
            self._select()
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
            .limit(limit)
        )
        if after is not None:
            position = (
                select(Message.sequence)
                .where(
                    Message.id == after.id,
                    Message.conversation_id == conversation_id,
                    self._tenant_filter(),
                )
                .scalar_subquery()
            )
            query = query.where(Message.sequence < position)
        return await self._all(query)

    async def latest_inbound(self, conversation_id: uuid.UUID) -> Message | None:
        """The most recent thing the customer said, by position.

        By `sequence`, so of several messages delivered together this is the
        one the customer sent last rather than whichever a random id tie-break
        happened to favour.
        """
        return await self._first(
            self._select()
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND,
            )
            .order_by(Message.sequence.desc())
        )

    async def find_provider_message(
        self,
        *,
        connection_id: uuid.UUID,
        provider_message_id: str,
    ) -> Message | None:
        """The message this provider id names on this connection (ADR-120)."""
        return await self._first(
            self._select().where(
                Message.connection_id == connection_id,
                Message.wa_message_id == provider_message_id,
            )
        )

    def record_inbound(
        self,
        *,
        conversation_id: uuid.UUID,
        connection_id: uuid.UUID,
        provider_message_id: str,
        kind: MessageKind,
        body: str | None,
        sent_at: datetime,
        action: ReplyAction | None = None,
    ) -> Message:
        """Stage a customer message the caller has established is new.

        Deciding *whether* a provider id is new is the projection's job, done
        before this, direction-aware and per connection (OMNI-005). This used
        to answer the question itself by returning *any* row with the id
        anywhere in the workspace - another conversation's, an outbound one -
        as a harmless duplicate, which is how Wasla's own reply came to be
        handed to an agent as a customer's message. The keys still decide a
        race: a concurrent insert of the same id on this connection fails at
        the flush, inside the caller's savepoint.
        """
        message = Message(
            # Named now rather than at insert: the caller hands this id to the
            # agent queue before anything flushes the row, and a job without it
            # is refused as unkeyed (TOOL-17) - which is how every live text
            # message came to be stored and never answered.
            id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            connection_id=connection_id,
            wa_message_id=provider_message_id,
            direction=MessageDirection.INBOUND,
            kind=kind,
            status=MessageStatus.RECEIVED,
            body=body,
            sent_at=sent_at,
            origin=MessageOrigin.CUSTOMER,
            # What the customer tapped, kept beside the words (OMNI-030).
            action_source=action.source if action is not None else None,
            action_payload=action.id_or_payload if action is not None else None,
            action_title=action.title if action is not None else None,
        )
        return self.add(message)

    async def advance_to_watermark(
        self,
        *,
        conversation_id: uuid.UUID,
        connection_id: uuid.UUID,
        status: MessageStatus,
        watermark: datetime,
    ) -> int:
        """Advance every outbound message sent at or before `watermark` - and nothing else.

        A provider that reports receipts as a watermark ("everything sent before
        this instant was read") names no message (OMNI-011). Only this
        conversation's outbound messages on this connection qualify, and only
        those already sent at or before the instant; each advances through
        `advance_status`, so the monotonic rules of a per-message receipt apply
        unchanged. A watermark can never move a message backwards, reach one
        sent after it, or touch another connection's or workspace's.
        """
        rows = await self._all(
            self._select().where(
                Message.conversation_id == conversation_id,
                Message.connection_id == connection_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.sent_at.is_not(None),
                Message.sent_at <= watermark,
            )
        )
        advanced = 0
        for message in rows:
            before = (message.status, message.delivered_at, message.read_at)
            self.advance_status(message, status=status, at=watermark)
            if (message.status, message.delivered_at, message.read_at) != before:
                advanced += 1
        return advanced

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
        origin: MessageOrigin,
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
            origin=origin,
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
        origin: MessageOrigin,
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
            origin=origin,
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
