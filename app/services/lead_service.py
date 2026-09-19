"""Lead management for one workspace.

Two kinds of caller reach this service and they are not equally trusted. A
person using the API states facts: they typed the customer's name, so it is the
name. An AI agent infers them from a sentence a customer wrote in passing, and
it is frequently right and occasionally confidently wrong.

Three rules follow from that, and they are the substance of this module:

**A human edit is sticky.** Any field a person sets is recorded in
`human_verified_fields`, and extraction skips every field in that list. The AI
fills in blanks and corrects its own earlier guesses; it never overwrites what
someone confirmed.

**Extraction never touches judgement.** `AGENT_WRITABLE_FIELDS` covers contact
details and stated interest. Status, score, assignment and tags are decisions,
and a decision is not something to infer from one message.

**Everything is on the record.** Each change writes a `LeadActivity` naming the
actor and carrying the previous value, so "why does this lead say the budget is
half a million" always has an answer.

**Every mutation is judged against the lead as committed.** Each one locks the
lead row and re-reads it before deciding anything (CRM-06, CRM-07). The rules
above held in sequence and not under concurrency: an extraction applied over a
correction a colleague committed mid-call, two colleagues editing different
fields each rewrote the verified list whole and one lost its protection, and
two status moves from the same state both succeeded - a won deal returned to
the pipeline. The agent's values are composed by the model with no lock held;
only their application takes one, and never across a provider call.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.db.models.conversation import Conversation, ConversationMode
from app.db.models.lead import (
    AGENT_WRITABLE_FIELDS,
    MAX_SCORE,
    MIN_SCORE,
    TERMINAL_STATUSES,
    ActorKind,
    Lead,
    LeadActivity,
    LeadActivityKind,
    LeadNote,
    LeadSource,
    LeadStatus,
    clamp_score,
)
from app.db.models.usage import UsageEventType
from app.repositories.conversation_repository import ContactRepository, ConversationRepository
from app.repositories.lead_repository import (
    LeadActivityRepository,
    LeadFilters,
    LeadNoteRepository,
    LeadRepository,
    LeadStatistics,
)
from app.repositories.membership_repository import MembershipRepository
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

#: The `error_code` of a status move whose `expected_status` is no longer the
#: lead's status: a colleague moved it first (CRM-07).
STALE_LEAD_STATUS = "stale_lead_status"
#: Shared with conversation assignment: the owner changed since the caller read it.
STALE_ASSIGNMENT = "stale_assignment"
#: A reopened lead would be the customer's second open one (CRM-13).
OPEN_LEAD_EXISTS = "open_lead_exists"

# Deliberately permissive. This rejects text that is obviously not an address
# rather than trying to decide deliverability, which no regular expression can.
# The cost of a false rejection is a lost lead; the cost of a false accept is a
# bounced email.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
# Digits, with the punctuation people actually type around them.
PHONE_PATTERN = re.compile(r"^\+?[\d\s().-]{6,31}$")

MAX_BUDGET = Decimal("999999999999.99")
CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")

MAX_NAME_LENGTH = 200
MAX_EMAIL_LENGTH = 320
MAX_PHONE_LENGTH = 32
MAX_INTEREST_LENGTH = 500
MAX_NOTE_LENGTH = 5000
MAX_TAGS = 25
MAX_TAG_LENGTH = 50


# The "leave this field alone" marker. A distinct sentinel rather than `None`,
# because null is a meaningful value here: clearing a budget someone entered by
# mistake has to be expressible, and an update type that cannot say "set this to
# nothing" forces the caller into a workaround.
UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class LeadUpdate:
    """Fields a caller wants changed. Anything left `UNSET` is untouched."""

    name: str | None | object = UNSET
    phone: str | None | object = UNSET
    email: str | None | object = UNSET
    interest: str | None | object = UNSET
    budget_amount: Decimal | str | int | float | None | object = UNSET
    budget_currency: str | None | object = UNSET
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ExtractedLead:
    """What an agent believes it learned from a conversation.

    Every field is optional because extraction is partial by nature: a customer
    gives their budget in one message and their name three messages later.
    """

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    interest: str | None = None
    budget_amount: str | float | int | None = None
    budget_currency: str | None = None

    def as_fields(self) -> dict[str, Any]:
        """The non-empty fields, keyed as they are on the model."""
        candidates = {
            "name": self.name,
            "phone": self.phone,
            "email": self.email,
            "interest": self.interest,
            "budget_amount": self.budget_amount,
            "budget_currency": self.budget_currency,
        }
        return {key: value for key, value in candidates.items() if value not in (None, "")}


@dataclass(frozen=True, slots=True)
class LeadCapture:
    """What one agent capture did to the customer's lead.

    `changed_fields` is empty when the model reported details the lead already
    held, which is the ordinary case for a model that calls the tool on every
    turn. The caller uses it to decide whether anything happened worth a row in
    the audit trail (TOOL-18).
    """

    lead: Lead
    created: bool
    changed_fields: frozenset[str]


class LeadService:
    """CRM operations for one workspace."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._leads = LeadRepository(session, tenant_id=tenant_id)
        self._notes = LeadNoteRepository(session, tenant_id=tenant_id)
        self._activities = LeadActivityRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._memberships = MembershipRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    # ------------------------------------------------------------------ reads

    async def list_leads(
        self,
        *,
        filters: LeadFilters | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Lead]:
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._leads.list_leads(filters=filters, limit=limit, after=after)
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    async def get_lead(self, lead_id: uuid.UUID) -> Lead:
        return await self._leads.require_by_id(lead_id)

    async def statistics(self, *, filters: LeadFilters | None = None) -> LeadStatistics:
        return await self._leads.statistics(filters=filters)

    async def list_notes(
        self,
        *,
        lead_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[LeadNote]:
        # Resolved first so another workspace's lead id answers not-found rather
        # than an empty list, which would confirm the id exists.
        await self._leads.require_by_id(lead_id)
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._notes.list_for_lead(lead_id=lead_id, limit=limit, after=after)
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    async def list_activity(
        self,
        *,
        lead_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[LeadActivity]:
        await self._leads.require_by_id(lead_id)
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._activities.list_for_lead(lead_id=lead_id, limit=limit, after=after)
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    # ----------------------------------------------------------------- writes

    async def create_lead(
        self,
        *,
        actor_id: uuid.UUID,
        source: LeadSource = LeadSource.MANUAL,
        contact_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        interest: str | None = None,
        budget_amount: Decimal | str | int | float | None = None,
        budget_currency: str | None = None,
        assigned_to_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> Lead:
        """Create a lead entered by a person.

        Every field supplied is marked human-verified straight away, so a later
        extraction leaves it alone. That is the point of entering it by hand.
        """
        if contact_id is not None:
            await self._contacts.require_by_id(contact_id)
            existing = await self._leads.get_active_for_contact(contact_id)
            if existing is not None:
                raise ConflictError(
                    "That customer already has an open lead. "
                    "Update it, or close it before opening another."
                )
        if conversation_id is not None:
            conversation = await self._conversations.require_by_id(conversation_id)
            if contact_id is not None and conversation.contact_id != contact_id:
                # Same workspace, different customer (CRM-14). Not a leak, so
                # the answer says what is wrong - and the database refuses the
                # row as well, through the three-column key on `leads`.
                raise ValidationError("That conversation is with a different customer.")
        if assigned_to_id is not None:
            await self._memberships.hold_active_for_user(assigned_to_id)

        fields = _validated(
            {
                "name": name,
                "phone": phone,
                "email": email,
                "interest": interest,
                "budget_amount": budget_amount,
                "budget_currency": budget_currency,
            }
        )
        now = datetime.now(UTC)
        lead = self._leads.create(
            source=source,
            contact_id=contact_id,
            conversation_id=conversation_id,
            assigned_to_id=assigned_to_id,
            tags=_validated_tags(tags),
            custom_fields=custom_fields or {},
            human_verified_fields=sorted(fields),
            last_activity_at=now,
            **fields,
        )
        await self._session.flush()

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.CREATED,
            summary="Lead created.",
            actor_id=actor_id,
            actor_kind=ActorKind.USER,
            data={"source": source.value},
        )
        self._usage.record(
            UsageEventType.LEAD_CREATED,
            occurred_at=now,
            meta={"lead_id": str(lead.id), "source": source.value},
        )
        logger.info(
            "lead.created",
            extra={"lead_id": str(lead.id), "tenant_id": str(self._tenant_id)},
        )
        return lead

    async def update_lead(
        self,
        *,
        lead_id: uuid.UUID,
        actor_id: uuid.UUID,
        update: LeadUpdate,
    ) -> Lead:
        """Apply a person's edit, marking every field they touched as verified.

        The lead is locked and re-read first, so the verified list extended
        here is the one the previous writer committed: two colleagues editing
        different fields at once end with both fields verified, never one
        (CRM-06).
        """
        lead = await self._leads.lock_by_id(lead_id)

        raw = {
            name: value
            for name, value in (
                ("name", update.name),
                ("phone", update.phone),
                ("email", update.email),
                ("interest", update.interest),
                ("budget_amount", update.budget_amount),
                ("budget_currency", update.budget_currency),
            )
            if value is not UNSET
        }
        fields = _validated(raw, allow_null=True)

        changes = _apply(lead, fields)

        if update.tags is not None:
            tags = _validated_tags(update.tags)
            if tags != lead.tags:
                changes["tags"] = {"from": list(lead.tags), "to": tags}
                lead.tags = tags
        if update.custom_fields is not None and update.custom_fields != lead.custom_fields:
            changes["custom_fields"] = {"from": lead.custom_fields, "to": update.custom_fields}
            lead.custom_fields = update.custom_fields

        # A person touched these, so extraction must not undo them later. Done
        # before the no-change return, because confirming a value that already
        # matched is still a person vouching for it - and the next extraction
        # must respect that. Includes a field they deliberately cleared: "this
        # customer has no email" is knowledge, and an agent guessing one back in
        # would erase it.
        #
        # A union with the list as committed, under the row lock - never a
        # whole-list overwrite from a stale read (CRM-06).
        lead.human_verified_fields = sorted(set(lead.human_verified_fields) | set(fields))

        if not changes:
            return lead

        lead.last_activity_at = datetime.now(UTC)

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.FIELDS_UPDATED,
            summary=f"Updated {', '.join(sorted(changes))}.",
            actor_id=actor_id,
            actor_kind=ActorKind.USER,
            data=_serialisable(changes),
        )
        return lead

    async def change_status(
        self,
        *,
        lead_id: uuid.UUID,
        status: LeadStatus,
        actor_id: uuid.UUID | None,
        actor_kind: ActorKind = ActorKind.USER,
        reason: str | None = None,
        expected_status: LeadStatus | None = None,
    ) -> Lead:
        """Move a lead through the pipeline, refusing illegal moves.

        A no-op is allowed and does nothing, so a retried request is safe.

        Judged against the status as committed, under the lead's row lock
        (CRM-07). Two moves from `qualified` - one to `won`, one to `proposal`
        - used to both succeed, returning a won deal to the pipeline with its
        closing date still set and two exits from one state in its timeline.
        Now the second is judged against what the first committed: with
        `expected_status` it is a 409; without it, a move the graph forbids
        from the new status is refused as any illegal move is.
        """
        lead = await self._leads.lock_by_id(lead_id)
        if expected_status is not None and lead.status is not expected_status:
            raise ConflictError(
                f"This lead is now {lead.status.value}. Refresh and try again.",
                error_code=STALE_LEAD_STATUS,
            )
        if status is lead.status:
            return lead

        if not lead.can_transition_to(status):
            raise ValidationError(f"A lead cannot move from {lead.status.value} to {status.value}.")

        if lead.is_closed and lead.contact_id is not None:
            # Reopening. The customer may have a newer open lead by now, and
            # `uq_leads_active_contact` allows one - which used to escape as a
            # 500 (CRM-13). Checked here for the ordinary case and caught
            # below for the race, because a newer lead can be opened between
            # this read and the write.
            newer = await self._leads.get_active_for_contact(lead.contact_id)
            if newer is not None and newer.id != lead.id:
                raise _open_lead_exists()

        previous = lead.status
        lead.status = status
        now = datetime.now(UTC)
        lead.last_activity_at = now

        if status is LeadStatus.QUALIFIED and lead.qualified_at is None:
            lead.qualified_at = now
        if status in TERMINAL_STATUSES:
            lead.closed_at = now
        elif previous in TERMINAL_STATUSES:
            # Reopened. The old closing date describes a decision that has been
            # reversed, so leaving it would misreport the pipeline.
            lead.closed_at = None

        if previous in TERMINAL_STATUSES:
            await self._reopen(lead)

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.STATUS_CHANGED,
            summary=f"Status changed from {previous.value} to {status.value}.",
            actor_id=actor_id,
            actor_kind=actor_kind,
            data={"from": previous.value, "to": status.value, "reason": reason},
        )
        logger.info(
            "lead.status_changed",
            extra={"lead_id": str(lead.id), "status": status.value},
        )
        return lead

    async def _reopen(self, lead: Lead) -> None:
        """Write a reopened lead now, so the one-open-lead index speaks here.

        Inside a savepoint: a newer lead opened between the pre-check and this
        write makes `uq_leads_active_contact` refuse it, and the request
        answers 409 having changed nothing instead of failing as a 500 at
        commit (CRM-13). Refreshed afterwards, because the flush leaves the
        server-maintained `updated_at` expired and the response reads it.
        """
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as error:
            if "uq_leads_active_contact" not in str(error.orig):
                raise
            raise _open_lead_exists() from error
        await self._session.refresh(lead)

    async def assign(
        self,
        *,
        lead_id: uuid.UUID,
        assigned_to_id: uuid.UUID | None,
        actor_id: uuid.UUID,
        expected_assigned_to_id: uuid.UUID | None,
    ) -> Lead:
        """Assign to a member of this workspace, or clear the assignment.

        Membership is verified, and share-locked, rather than assumed: the id
        arrives in a request body, and it must not be somebody outside the
        workspace or somebody whose removal is committing now (CRM-11).

        `expected_assigned_to_id` is the owner the caller is replacing; if it is
        not the owner when the write lands, a 409 that changes nothing
        (PD-CRM-4).
        """
        if assigned_to_id is not None:
            await self._memberships.hold_active_for_user(assigned_to_id)
        lead = await self._leads.lock_by_id(lead_id)
        if lead.assigned_to_id != expected_assigned_to_id:
            raise ConflictError(
                "This lead's owner changed since you looked. Refresh and try again.",
                error_code=STALE_ASSIGNMENT,
            )

        if assigned_to_id == lead.assigned_to_id:
            return lead

        previous = lead.assigned_to_id
        lead.assigned_to_id = assigned_to_id
        lead.last_activity_at = datetime.now(UTC)

        self._activities.record(
            lead_id=lead.id,
            kind=(
                LeadActivityKind.ASSIGNED
                if assigned_to_id is not None
                else LeadActivityKind.UNASSIGNED
            ),
            summary=("Assigned." if assigned_to_id is not None else "Assignment cleared."),
            actor_id=actor_id,
            actor_kind=ActorKind.USER,
            data={
                "from": str(previous) if previous else None,
                "to": str(assigned_to_id) if assigned_to_id else None,
            },
        )
        return lead

    async def release_member(self, *, user_id: uuid.UUID, actor_id: uuid.UUID | None) -> int:
        """Unassign every lead a departing member owns here. Returns how many.

        Inside the removal's transaction, after the membership row is revoked
        (PD-CRM-2). The lead goes to nobody rather than to whoever removed the
        member, and its timeline records why.
        """
        released = await self._leads.lock_assigned_to(user_id)
        now = datetime.now(UTC)
        for lead in released:
            lead.assigned_to_id = None
            lead.last_activity_at = now
            self._activities.record(
                lead_id=lead.id,
                kind=LeadActivityKind.UNASSIGNED,
                summary="Assignment cleared: the owner left the workspace.",
                actor_id=actor_id,
                actor_kind=ActorKind.USER if actor_id is not None else ActorKind.SYSTEM,
                data={"from": str(user_id), "to": None, "cause": "member_revoked"},
            )
        return len(released)

    async def set_score(
        self,
        *,
        lead_id: uuid.UUID,
        score: int,
        actor_id: uuid.UUID | None,
        actor_kind: ActorKind = ActorKind.USER,
    ) -> Lead:
        """Set the qualification score, clamped to its bounds."""
        lead = await self._leads.lock_by_id(lead_id)
        value = clamp_score(score)
        if value == lead.score:
            return lead

        previous = lead.score
        lead.score = value
        lead.last_activity_at = datetime.now(UTC)

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.SCORE_CHANGED,
            summary=f"Score changed from {previous} to {value}.",
            actor_id=actor_id,
            actor_kind=actor_kind,
            data={"from": previous, "to": value},
        )
        return lead

    async def add_note(
        self,
        *,
        lead_id: uuid.UUID,
        body: str,
        author_id: uuid.UUID | None,
        author_kind: ActorKind = ActorKind.USER,
    ) -> LeadNote:
        """Attach an internal note. Never sent to the customer."""
        lead = await self._leads.lock_by_id(lead_id)

        text = body.strip()
        if not text:
            raise ValidationError("A note cannot be empty.")
        if len(text) > MAX_NOTE_LENGTH:
            raise ValidationError(f"A note cannot exceed {MAX_NOTE_LENGTH} characters.")

        note = self._notes.create(
            lead_id=lead.id,
            body=text,
            author_id=author_id,
            author_kind=author_kind,
        )
        lead.last_activity_at = datetime.now(UTC)
        await self._session.flush()

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.NOTE_ADDED,
            summary="Note added.",
            actor_id=author_id,
            actor_kind=author_kind,
            data={"note_id": str(note.id)},
        )
        return note

    # ------------------------------------------------------------- extraction

    async def capture_from_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        extracted: ExtractedLead,
    ) -> LeadCapture:
        """Record what an agent learned, into the customer's open lead.

        The lead is found from the conversation's contact, never named by the
        model. That is what makes this idempotent: calling it five times in one
        conversation updates one lead five times instead of creating five.

        Fields a person has verified are left alone, and fields outside
        `AGENT_WRITABLE_FIELDS` are not reachable from here at all.

        Returns what happened as well as the row, because "saved" and "changed
        something" are different facts and only the caller knows which it needs.
        The agent tool needs the second: forty identical calls used to write
        forty audit rows for one lead (TOOL-18).
        """
        conversation = await self._fresh_conversation(conversation_id)

        if conversation.mode is ConversationMode.HUMAN:
            # Defence in depth. The orchestrator already refuses to run an agent
            # on a human-handled conversation, but a job enqueued before the
            # handoff can still be picked up after it, and a colleague mid-call
            # with the customer should not have the record edited underneath
            # them by a model working from older messages.
            raise ConflictError("This conversation is handled by a colleague.")

        # Locked and re-read: the values were composed by the model before a
        # colleague may have corrected them, and they are applied against the
        # lead as it is now, including the fields that correction just
        # verified (CRM-06). The lock is released by the turn's next commit,
        # which `released` takes before any provider call.
        lead = await self._leads.get_active_for_contact(conversation.contact_id, for_update=True)

        if lead is None:
            created = await self._create_from_conversation(conversation, extracted)
            if created is not None:
                return created
            # Somebody else opened the customer's lead between the read above
            # and the insert. Fall through and update theirs.
            lead = await self._leads.get_active_for_contact(
                conversation.contact_id,
                for_update=True,
            )
            if lead is None:  # pragma: no cover - the index says this cannot happen
                raise ConflictError("This customer's lead could not be resolved.")

        return self._update_from_extraction(lead, conversation, extracted)

    async def _fresh_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        """The conversation as the database holds it *now* (TOOL-21).

        `populate_existing`, and that is the whole point of this method. The
        turn's session loaded this conversation before the inference and
        `expire_on_commit=False` keeps the loaded attributes readable
        afterwards - so an ordinary `select` returning the mapped instance would
        hand back the snapshot from before the provider was called, and the
        takeover guard below would be reading a picture of the world minutes
        old.

        It held before this only by accident: nothing kept a strong reference to
        the instance across the inference, so the identity map had usually
        dropped it and the re-select genuinely re-read. That is a property of
        garbage collection rather than of the code, and a refactor keeping the
        object alive would have turned a proved guard into a stale read with no
        test to notice.
        """
        return await self._conversations.require_by_id(
            conversation_id,
            populate_existing=True,
        )

    async def _create_from_conversation(
        self,
        conversation: Conversation,
        extracted: ExtractedLead,
    ) -> LeadCapture | None:
        """Open the customer's lead, or answer None because somebody else did.

        **Inside a savepoint, and the savepoint is the point** (TOOL-02). Two
        turns of one conversation are ordinary: a customer writing twice in a
        few seconds produces two, and this product deliberately does not
        coalesce them (AI-08). Both can reach this line, and
        `uq_leads_active_contact` lets exactly one insert succeed - which used
        to mean the loser's `IntegrityError` escaped the tool, aborted the
        turn's whole transaction, and left the second customer message answered
        by silence.

        Converging instead of crashing needs the failed insert rolled back to a
        point the rest of the turn survives, which is what `begin_nested` gives:
        after the rollback this session is usable and the caller updates the
        winner's row. The unique index stays; it is what makes the outcome
        correct rather than merely uncrashed.
        """
        fields = _validated(
            _agent_writable(extracted.as_fields()),
            # An agent's guess is dropped rather than raised on: it is not a
            # caller mistake to be reported, and failing the whole capture
            # over one malformed phone number would lose the rest.
            lenient=True,
        )
        try:
            async with self._session.begin_nested():
                lead = self._leads.create(
                    source=LeadSource.AGENT,
                    contact_id=conversation.contact_id,
                    conversation_id=conversation.id,
                    last_activity_at=datetime.now(UTC),
                    **fields,
                )
                await self._session.flush()
        except IntegrityError:
            logger.info(
                "lead.create_raced",
                extra={
                    "event": "lead.create_raced",
                    "conversation_id": str(conversation.id),
                },
            )
            return None

        self._activities.record(
            lead_id=lead.id,
            kind=LeadActivityKind.CREATED,
            summary="Lead created from a conversation.",
            actor_kind=ActorKind.AGENT,
            data={"conversation_id": str(conversation.id), "fields": sorted(fields)},
        )
        # Only the branch that created one. An extraction that updated an
        # existing lead has captured nothing new, and counting it would
        # make "leads created" grow every time a customer says anything.
        self._usage.record(
            UsageEventType.LEAD_CREATED,
            meta={"lead_id": str(lead.id), "source": LeadSource.AGENT.value},
        )
        logger.info(
            "lead.captured",
            extra={"lead_id": str(lead.id), "conversation_id": str(conversation.id)},
        )
        return LeadCapture(lead=lead, created=True, changed_fields=frozenset(fields))

    def _update_from_extraction(
        self,
        lead: Lead,
        conversation: Conversation,
        extracted: ExtractedLead,
    ) -> LeadCapture:
        proposed = _agent_writable(extracted.as_fields())
        # Read from the row as locked, never from a copy loaded earlier: this
        # is the list a colleague's correction may have extended a moment ago.
        protected = set(lead.human_verified_fields)
        # The core rule: a human's entry wins over a model's inference.
        allowed = {key: value for key, value in proposed.items() if key not in protected}
        skipped = sorted(set(proposed) - set(allowed))

        fields = _validated(allowed, lenient=True)
        changes = _apply(lead, fields)

        if skipped:
            logger.info(
                "lead.extraction_skipped_verified_fields",
                extra={"lead_id": str(lead.id), "fields": skipped},
            )

        if changes:
            lead.last_activity_at = datetime.now(UTC)
            self._activities.record(
                lead_id=lead.id,
                kind=LeadActivityKind.FIELDS_UPDATED,
                summary=f"Agent updated {', '.join(sorted(changes))}.",
                actor_kind=ActorKind.AGENT,
                data=_serialisable({**changes, "skipped_verified": skipped}),
            )

        logger.info(
            "lead.captured",
            extra={
                "lead_id": str(lead.id),
                "conversation_id": str(conversation.id),
                "is_new": False,
            },
        )
        return LeadCapture(lead=lead, created=False, changed_fields=frozenset(changes))


def _open_lead_exists() -> ConflictError:
    return ConflictError(
        "This customer already has a newer open lead. Update that one instead.",
        error_code=OPEN_LEAD_EXISTS,
    )


def _agent_writable(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop anything an agent is not allowed to set.

    Belt and braces: the tool schema does not offer these fields, so reaching
    here means something changed. Filtering rather than raising keeps a schema
    change from becoming an outage.
    """
    return {key: value for key, value in fields.items() if key in AGENT_WRITABLE_FIELDS}


def _apply(lead: Lead, fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Set fields on the lead, returning what actually changed.

    Comparing before assigning is what keeps the activity log meaningful: a
    save that changed nothing should not appear in the customer's history as
    though someone did something.
    """
    changes: dict[str, dict[str, Any]] = {}
    for key, value in fields.items():
        current = getattr(lead, key)
        if current == value:
            continue
        changes[key] = {"from": current, "to": value}
        setattr(lead, key, value)
    return changes


def _validated(
    fields: dict[str, Any],
    *,
    allow_null: bool = False,
    lenient: bool = False,
) -> dict[str, Any]:
    """Clean and check lead fields.

    `lenient` is for values a model produced: a bad one is dropped so the rest
    of the extraction still lands. Without it a bad one raises, which is what a
    person typing into a form should get.
    """
    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            if allow_null:
                cleaned[key] = None
            continue
        try:
            cleaned[key] = _validate_field(key, value)
        except ValidationError:
            if not lenient:
                raise
            logger.info("lead.field_rejected", extra={"field": key})
    return cleaned


def _validate_field(key: str, value: Any) -> Any:
    if key == "budget_amount":
        return _validated_budget(value)
    if key == "budget_currency":
        text = str(value).strip().upper()
        if not CURRENCY_PATTERN.match(text):
            raise ValidationError("A currency must be a three-letter code, such as EGP.")
        return text

    text = str(value).strip()
    if not text:
        raise ValidationError(f"{key} cannot be blank.")

    if key == "email":
        if len(text) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(text):
            raise ValidationError("That does not look like an email address.")
        return text.lower()
    if key == "phone":
        if len(text) > MAX_PHONE_LENGTH or not PHONE_PATTERN.match(text):
            raise ValidationError("That does not look like a phone number.")
        return text
    if key == "name":
        return text[:MAX_NAME_LENGTH]
    if key == "interest":
        return text[:MAX_INTEREST_LENGTH]
    return text


def _validated_budget(value: Any) -> Decimal:
    """Parse a budget, refusing what cannot be money.

    Models write budgets as "500k", "500,000" and "500000 EGP". Only the last
    two are handled: guessing at multipliers means eventually reading "500k" as
    500 for one customer and 500,000 for another, and a wrong budget silently
    reprioritises a real sales pipeline. An unparsed value is dropped, and the
    conversation still has the customer's own words in it.
    """
    if isinstance(value, bool):
        raise ValidationError("A budget must be a number.")
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as error:
        raise ValidationError("A budget must be a number.") from error

    if not amount.is_finite():
        raise ValidationError("A budget must be a number.")
    if amount < 0:
        raise ValidationError("A budget cannot be negative.")
    if amount > MAX_BUDGET:
        raise ValidationError("That budget is larger than this system records.")
    return amount.quantize(Decimal("0.01"))


def _validated_tags(tags: list[str] | None) -> list[str]:
    """Normalise tags: trimmed, lowercased, deduplicated, bounded."""
    if not tags:
        return []
    cleaned: list[str] = []
    for tag in tags:
        text = str(tag).strip().lower()
        if not text:
            continue
        if len(text) > MAX_TAG_LENGTH:
            raise ValidationError(f"A tag cannot exceed {MAX_TAG_LENGTH} characters.")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_TAGS:
        raise ValidationError(f"A lead cannot carry more than {MAX_TAGS} tags.")
    return cleaned


def _serialisable(value: Any) -> Any:
    """Make a change record safe to store as JSONB.

    Decimals and UUIDs are not JSON, and the activity log must not be the thing
    that fails a request that otherwise succeeded.
    """
    if isinstance(value, dict):
        return {key: _serialisable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialisable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value


__all__ = [
    "MAX_SCORE",
    "MIN_SCORE",
    "OPEN_LEAD_EXISTS",
    "STALE_ASSIGNMENT",
    "STALE_LEAD_STATUS",
    "UNSET",
    "ExtractedLead",
    "LeadService",
    "LeadUpdate",
]
