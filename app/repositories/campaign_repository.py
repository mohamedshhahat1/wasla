"""Data access for campaigns, their recipients and their audiences.

Four classes, and the shapes differ for reasons worth stating.

`CampaignRepository` and `CampaignRecipientRepository` are tenant-scoped like
everything a request touches. `AudienceRepository` is too, and is the only place
the rule that defines a legitimate audience is written down.

`DueCampaignClaim` is **not** scoped, because the worker sweeps every workspace
on a timer and has no tenant to be confined to. It is a separate class with a
name that says so, exactly as `DueFollowUpClaim` is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, and_, delete, false, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.pagination import Cursor
from app.core.telemetry import record_campaign_event
from app.db.models.campaign import (
    Campaign,
    CampaignCreateKey,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.db.models.conversation import Contact, Conversation, Message, MessageStatus
from app.db.models.lead import Lead, LeadStatus
from app.repositories.base import BaseRepository, TenantScopedRepository, escape_like
from app.repositories.consent_repository import refused

logger = get_logger(__name__)

# How many campaigns one sweep takes on. Small: each one then sends a batch, and
# a worker holding twenty campaigns' rows locked while it talks to Meta is
# holding them from every other replica.
DEFAULT_CAMPAIGN_CLAIM_LIMIT = 5

# How many recipients one campaign sends to per sweep, before the rate limit is
# consulted again. The real limit is `messages_per_minute`; this only bounds how
# much work is held under one lock.
DEFAULT_RECIPIENT_BATCH = 50


@dataclass(frozen=True, slots=True)
class AudienceFilter:
    """Who a campaign is for, within the people it is allowed to reach.

    Every field narrows. None of them widens: the base population is always the
    contacts this workspace has an existing conversation with on the sending
    number, and no filter can add someone outside it.
    """

    last_inbound_within_days: int | None = None
    lead_statuses: tuple[LeadStatus, ...] = ()
    contact_ids: tuple[uuid.UUID, ...] = ()
    #: A segment and filters naming lead statuses with none in common (PG-06):
    #: what was asked for reaches nobody.
    nobody: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The record kept on the campaign, so a person can read what was targeted."""
        record: dict[str, Any] = {
            "last_inbound_within_days": self.last_inbound_within_days,
            "lead_statuses": [status.value for status in self.lead_statuses],
            "contact_ids": [str(contact_id) for contact_id in self.contact_ids],
        }
        if self.nobody:
            record["nobody"] = True
        return record


#: A recipient without a reason code - sent, or written before reason codes.
UNRECORDED_REASON: Final = "unrecorded"


@dataclass(frozen=True, slots=True)
class ExportRow:
    """One recipient as the CSV export writes it (PG-14)."""

    id: uuid.UUID
    contact_name: str | None
    phone: str | None
    status: RecipientStatus
    reason_code: str | None
    provider_code: int | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    replied_at: datetime | None
    opted_out_after_at: datetime | None


@dataclass(frozen=True, slots=True)
class CampaignStatistics:
    """What became of one campaign's messages.

    `delivered` and `read` are counted from the message rows the webhook
    advances, not from anything this system writes at send time. A message Meta
    accepted is `sent`; whether it arrived is Meta's news to bring.
    """

    pending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    delivered: int = 0
    read: int = 0
    #: Sent - Meta accepted them - and reported `failed` afterwards (CMP-10).
    failed_after_send: int = 0
    #: Accepted and held by Meta's portfolio pacing (LIM-08).
    held: int = 0
    #: Answered on the campaign's number within the attribution window (PG-10).
    replied: int = 0
    #: Opted out by their own decision within the window after the send.
    opted_out_after: int = 0
    #: `(status, reason_code, provider_code, count)`, every recipient once.
    reasons: tuple[tuple[str, str, int | None, int], ...] = ()

    @property
    def total(self) -> int:
        return self.pending + self.sent + self.failed + self.skipped


def _after_created(after: Cursor) -> ColumnElement[bool]:
    """Rows following `after` under ``ORDER BY created_at DESC, id DESC``."""
    if after.sort_value is None:
        return Campaign.id < after.id
    return or_(
        Campaign.created_at < after.sort_value,
        and_(Campaign.created_at == after.sort_value, Campaign.id < after.id),
    )


class CampaignRepository(TenantScopedRepository[Campaign]):
    """Campaigns of one workspace."""

    model = Campaign

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Campaign.tenant_id == self.tenant_id

    async def get_by_id(self, campaign_id: uuid.UUID) -> Campaign | None:
        return await self._first(self._select().where(Campaign.id == campaign_id))

    async def is_cancelled(self, campaign_id: uuid.UUID) -> bool:
        """Whether this campaign has been cancelled since the batch was claimed.

        A scalar read of the column rather than a refresh of the mapped object,
        deliberately. `cancel()` runs in a different request and a different
        transaction, so the object the sweep is holding is a snapshot taken
        before the batch began - and a `select` returning the mapped instance
        would hand back the attributes it was loaded with. This is the same
        identity-map trap `AgentOrchestrator._taken_over` avoids, for the same
        reason and by the same means.
        """
        status = await self.session.scalar(
            select(Campaign.status).where(
                Campaign.id == campaign_id,
                self._tenant_filter(),
            )
        )
        return status is CampaignStatus.CANCELLED

    async def lock_by_id(self, campaign_id: uuid.UUID) -> Campaign:
        """One campaign, locked until the transaction ends and read as it is now.

        `schedule()` and `set_audience()` both take it (CMP-04), so a
        materialisation and a schedule of one campaign are ordered rather than
        interleaved. `populate_existing`, because a request may already hold
        the row in its identity map as it was loaded.
        """
        return await self._require(
            self._select()
            .where(Campaign.id == campaign_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def lock_live(self) -> list[Campaign]:
        """This workspace's scheduled and running campaigns, locked, as they are now.

        For a lifecycle transition pausing them (CMP-02). Waits for a worker
        that is between a recipient's check and its send intent, so the pause
        lands after that one send and the worker's next check sees it.
        """
        statement = (
            self._select()
            .where(Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]))
            .order_by(Campaign.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._all(statement)

    async def current_status(self, campaign_id: uuid.UUID) -> CampaignStatus | None:
        """The campaign's status as committed now, never as a loaded object remembers it."""
        status: CampaignStatus | None = await self.session.scalar(
            select(Campaign.status).where(Campaign.id == campaign_id, self._tenant_filter())
        )
        return status

    async def hold(
        self,
        campaign_id: uuid.UUID,
        *,
        lease_token: uuid.UUID | None,
        lease_until: datetime | None,
    ) -> bool:
        """Whether this worker may go on sending this campaign, renewing its lease if so.

        One conditional statement (CMP-01, CMP-08): the campaign is still
        `scheduled` or `running` - a pause or a cancel committed since the
        previous recipient answers False - and, with a `lease_token`, still
        carries this worker's claim. Asked before every recipient, so a pause
        stops the batch at the next person rather than after all of them, and a
        worker whose lease was taken over sends nothing more.

        The renewal is what lets a lease be shorter than a batch: it has to
        outlast one recipient's send, not fifty. The row stays locked until
        the send intent commits, so a concurrent pause waits for that commit
        and is seen at the next recipient.
        """
        statement = (
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                self._tenant_filter(),
                Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
            )
            .returning(Campaign.id)
            .execution_options(synchronize_session=False)
        )
        if lease_token is not None:
            statement = statement.where(Campaign.lease_token == lease_token)
        if lease_until is not None:
            statement = statement.values(next_send_at=lease_until)
        else:
            # A no-op write that still takes the row lock and answers.
            statement = statement.values(status=Campaign.status)
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def record_header_upload(
        self,
        campaign_id: uuid.UUID,
        *,
        lease_token: uuid.UUID | None,
        provider_media_id: str,
        uploaded_at: datetime,
        account_id: uuid.UUID,
    ) -> bool:
        """Write Meta's id for the campaign's header file - only while the claim is the caller's.

        One conditional statement (HM-04), the rule `record_batch_failure`
        follows: the campaign is still live and, with a `lease_token`, still
        carries this worker's claim. A worker whose lease was taken over while
        it uploaded writes nothing, and its upload is never sent: an unused
        file at Meta, which expires (HM-04).
        """
        statement = (
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                self._tenant_filter(),
                Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
            )
            .values(
                header_provider_media_id=provider_media_id,
                header_provider_uploaded_at=uploaded_at,
                header_provider_account_id=account_id,
            )
            .returning(Campaign.id)
            .execution_options(synchronize_session=False)
        )
        if lease_token is not None:
            statement = statement.where(Campaign.lease_token == lease_token)
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def require_by_id(self, campaign_id: uuid.UUID) -> Campaign:
        return await self._require(self._select().where(Campaign.id == campaign_id))

    async def list_campaigns(
        self,
        *,
        statuses: tuple[CampaignStatus, ...] = (),
        account_id: uuid.UUID | None = None,
        limit: int = 50,
        after: Cursor | None = None,
        name_contains: str | None = None,
    ) -> list[Campaign]:
        """Newest first, paged by keyset.

        `name_contains` matches anywhere in the name, ignoring case, as typed:
        `%` and `_` are the characters they are, not wildcards (PG-01).
        """
        query = self._select()
        if statuses:
            query = query.where(Campaign.status.in_(statuses))
        if account_id is not None:
            query = query.where(Campaign.account_id == account_id)
        if name_contains:
            query = query.where(Campaign.name.ilike(f"%{escape_like(name_contains)}%", escape="\\"))
        if after is not None:
            query = query.where(_after_created(after))
        return await self._all(
            query.order_by(Campaign.created_at.desc(), Campaign.id.desc()).limit(limit)
        )

    def create(
        self,
        *,
        account_id: uuid.UUID,
        template_id: uuid.UUID,
        name: str,
        description: str | None,
        variables: list[str] | None,
        messages_per_minute: int,
        created_by_id: uuid.UUID | None,
    ) -> Campaign:
        """Stage a draft. The tenant comes from this repository, never the caller."""
        return self.add(
            Campaign(
                tenant_id=self.tenant_id,
                account_id=account_id,
                template_id=template_id,
                name=name,
                description=description,
                status=CampaignStatus.DRAFT,
                variables=variables,
                audience_size=0,
                messages_per_minute=messages_per_minute,
                created_by_id=created_by_id,
            )
        )

    async def created_with_key(
        self, key: str, *, since: datetime
    ) -> tuple[CampaignCreateKey, Campaign | None] | None:
        """The draft a create key made in the last day, and the key's row (PG-08)."""
        row = await self.session.scalar(
            select(CampaignCreateKey).where(
                CampaignCreateKey.tenant_id == self.tenant_id,
                CampaignCreateKey.key == key,
                CampaignCreateKey.created_at >= since,
            )
        )
        if row is None:
            return None
        return row, await self.get_by_id(row.campaign_id)

    async def claim_create_key(
        self, *, key: str, campaign_id: uuid.UUID, request_hash: str, stale_before: datetime
    ) -> bool:
        """Record that `key` made `campaign_id`; False when another request holds it.

        A key older than a day is forgotten first. The insert waits for a
        concurrent request holding the same key to commit or roll back, so two
        retries racing each other make exactly one draft.
        """
        await self.session.execute(
            delete(CampaignCreateKey).where(
                CampaignCreateKey.tenant_id == self.tenant_id,
                CampaignCreateKey.key == key,
                CampaignCreateKey.created_at < stale_before,
            )
        )
        result = await self.session.execute(
            pg_insert(CampaignCreateKey)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                key=key,
                campaign_id=campaign_id,
                request_hash=request_hash,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
            .returning(CampaignCreateKey.id)
        )
        return result.scalar_one_or_none() is not None


async def prune_create_keys(session: AsyncSession, *, before: datetime) -> int:
    """Delete every workspace's create keys older than `before` (PG-08, retention).

    Unscoped on purpose: the retention sweep runs for the whole platform, and a
    key past its day names nothing anybody can ask for again.
    """
    result = await session.execute(
        delete(CampaignCreateKey)
        .where(CampaignCreateKey.created_at < before)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


class CampaignRecipientRepository(TenantScopedRepository[CampaignRecipient]):
    """One campaign's recipients, within one workspace."""

    model = CampaignRecipient

    def _tenant_filter(self) -> ColumnElement[bool]:
        return CampaignRecipient.tenant_id == self.tenant_id

    async def list_for_campaign(
        self,
        campaign_id: uuid.UUID,
        *,
        status: RecipientStatus | None = None,
        reason: str | None = None,
        limit: int = 100,
        after: uuid.UUID | None = None,
    ) -> list[CampaignRecipient]:
        """A page of recipients by id - a keyset, so every page is cheap (CMP-13)."""
        query = self._select().where(CampaignRecipient.campaign_id == campaign_id)
        if status is not None:
            query = query.where(CampaignRecipient.status == status)
        if reason is not None:
            query = query.where(CampaignRecipient.reason_code == reason)
        if after is not None:
            query = query.where(CampaignRecipient.id > after)
        return await self._all(query.order_by(CampaignRecipient.id).limit(limit))

    async def existing_contact_ids(self, campaign_id: uuid.UUID) -> set[uuid.UUID]:
        """Who is already on the list, so rebuilding one adds rather than duplicates."""
        result = await self.session.execute(
            select(CampaignRecipient.contact_id).where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
        )
        return set(result.scalars().all())

    async def pending_in_other_live_campaigns(self, campaign_id: uuid.UUID) -> int:
        """Recipients other scheduled or running campaigns have yet to reach.

        The campaign allowance's reservation (BILL-08): these sends are already
        promised, and are metered only as each one goes out.
        """
        from app.db.models.campaign import Campaign, CampaignStatus

        result = await self.session.execute(
            select(func.count())
            .select_from(CampaignRecipient)
            .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id != campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
                Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
            )
        )
        return int(result.scalar_one())

    async def pending_count(self, campaign_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
            )
        )
        return int(result.scalar_one())

    def create(
        self,
        *,
        campaign_id: uuid.UUID,
        contact_id: uuid.UUID,
        participant_identity_id: uuid.UUID | None = None,
    ) -> CampaignRecipient:
        return self.add(
            CampaignRecipient(
                tenant_id=self.tenant_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                participant_identity_id=participant_identity_id,
                status=RecipientStatus.PENDING,
                attempts=0,
            )
        )

    async def claim_pending(
        self,
        campaign_id: uuid.UUID,
        *,
        limit: int = DEFAULT_RECIPIENT_BATCH,
        now: datetime | None = None,
    ) -> list[CampaignRecipient]:
        """Lock and return recipients still waiting for their copy.

        ``FOR UPDATE SKIP LOCKED`` for the reason follow-ups use it, with more
        at stake: two replicas working one campaign would otherwise both read
        the same pending rows and send ten thousand people the message twice.
        The lock ends at the first send commit; `take` is what holds per
        person after that (CMP-01).

        A recipient deferred by Meta's pair rate limit is skipped until its
        `not_before` (CMP-18), so one person does not hold up the rest.
        """
        moment = now or datetime.now(UTC)
        statement = (
            select(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
                or_(
                    CampaignRecipient.not_before.is_(None),
                    CampaignRecipient.not_before <= moment,
                ),
            )
            .order_by(CampaignRecipient.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def skip_pending(self, campaign_id: uuid.UUID, *, reason: str, detail: str) -> int:
        """Skip everybody still pending, in one statement (LIM-06). Returns how many.

        Rows another worker holds between its check and its send intent are
        waited for, not skipped from under it: the statement takes each row's
        lock as it updates it.
        """
        result = await self.session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
            )
            .values(status=RecipientStatus.SKIPPED, reason_code=reason, last_error=detail)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def ever_sent(self, campaign_id: uuid.UUID) -> bool:
        """Whether anybody on this list was ever handed to Meta (PG-04)."""
        found = await self.session.scalar(
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                or_(
                    CampaignRecipient.sent_at.is_not(None),
                    CampaignRecipient.message_id.is_not(None),
                    CampaignRecipient.attempts > 0,
                ),
            )
            .limit(1)
        )
        return found is not None

    async def delete_all(self, campaign_id: uuid.UUID) -> int:
        """Drop a draft's written list: its rules stay on the campaign (PG-03, PG-04)."""
        result = await self.session.execute(
            delete(CampaignRecipient).where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def rendered_for(
        self, campaign_id: uuid.UUID, contact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any] | None]:
        """The values written for these contacts in this campaign, by contact (PER-04)."""
        rows = (
            await self.session.execute(
                select(CampaignRecipient.contact_id, CampaignRecipient.rendered_parameters).where(
                    CampaignRecipient.tenant_id == self.tenant_id,
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.contact_id.in_(list(contact_ids)),
                )
            )
        ).all()
        found: dict[uuid.UUID, dict[str, Any] | None] = {}
        for contact_id, rendered in rows:
            found[contact_id] = rendered
        return found

    async def capped_pending(self, campaign_id: uuid.UUID, *, since: datetime) -> bool:
        """Whether a pending person was sent a marketing message after `since` (FREQ-01)."""
        found = await self.session.scalar(
            select(CampaignRecipient.id)
            .join(Contact, Contact.id == CampaignRecipient.contact_id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
                Contact.last_marketing_sent_at > since,
            )
            .limit(1)
        )
        return found is not None

    async def earliest_deferral(self, campaign_id: uuid.UUID) -> datetime | None:
        """When the first pending recipient deferred by a pair limit may be asked (CMP-18)."""
        moment: datetime | None = await self.session.scalar(
            select(func.min(CampaignRecipient.not_before)).where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
                CampaignRecipient.not_before.is_not(None),
            )
        )
        return moment

    async def take(self, recipient: CampaignRecipient) -> CampaignRecipient | None:
        """Own one pending recipient for one send, or None if that is not this worker's to do.

        **The decisive guard against a second copy across workers** (CMP-01,
        ADR-134). `claim_pending` locks the batch, but those locks end at the
        first send commit (ADR-093 TX1), and the rest of the batch is then a
        set of in-memory snapshots. This re-reads the row in one statement that
        both checks and locks it: still `pending`, and not locked by another
        worker that is between its own check and its send intent. The row is
        refreshed from the database (`populate_existing`), so every decision
        after this - including the abandon rule for a pending row that already
        names a message - is taken from the row as it is, never from the
        snapshot.

        The lock is held until the send intent commits, by which time the row
        names its message; a second worker reaching it afterwards sees that
        link and abandons rather than sends. None means another worker owns
        it, or finished it: skipped with no send and no attempt spent.
        """
        statement = (
            select(CampaignRecipient)
            .where(
                CampaignRecipient.id == recipient.id,
                CampaignRecipient.campaign_id == recipient.campaign_id,
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(statement)).scalars().one_or_none()

    async def statistics(self, campaign_id: uuid.UUID) -> CampaignStatistics:
        """Counts per outcome, plus what Meta has since said about delivery."""
        return await self.statistics_of(CampaignRecipient.campaign_id == campaign_id)

    async def statistics_of(self, campaigns: ColumnElement[bool]) -> CampaignStatistics:
        """The statistics of every recipient `campaigns` selects, in this workspace.

        One campaign's (`campaign_id = ...`) or many (the KPI summary,
        PG-11): the same queries, so the summary equals the sum of the
        campaigns' statistics by construction rather than by care.

        Separate queries rather than one. The outcome counts come from the
        recipient rows; delivery and read come from the message rows a webhook
        advances, and joining them into a single grouped query would
        double-count the recipients that have no message yet.
        """
        scope = and_(CampaignRecipient.tenant_id == self.tenant_id, campaigns)
        counts = await self.session.execute(
            select(
                CampaignRecipient.status,
                func.count(),
                func.count(CampaignRecipient.replied_at),
                func.count(CampaignRecipient.opted_out_after_at),
            )
            .where(scope)
            .group_by(CampaignRecipient.status)
        )
        tally: dict[RecipientStatus, int] = {}
        replied = opted_out_after = 0
        for status, count, answered, refused_after in counts.all():
            tally[status] = int(count)
            replied += int(answered)
            opted_out_after += int(refused_after)

        delivery = await self.session.execute(
            select(Message.status, func.count())
            .join(CampaignRecipient, CampaignRecipient.message_id == Message.id)
            .where(scope)
            .group_by(Message.status)
        )
        by_message = {status: int(count) for status, count in delivery.all()}
        read = by_message.get(MessageStatus.READ, 0)
        failed_after_send = await self.session.scalar(
            select(func.count())
            .select_from(CampaignRecipient)
            .join(Message, Message.id == CampaignRecipient.message_id)
            .where(
                scope,
                CampaignRecipient.status == RecipientStatus.SENT,
                Message.status == MessageStatus.FAILED,
            )
        )
        held = await self.session.scalar(
            select(func.count())
            .select_from(CampaignRecipient)
            .join(Message, Message.id == CampaignRecipient.message_id)
            .where(scope, Message.provider_held_at.is_not(None))
        )
        # Why people were not reached, or what Meta said: every recipient once,
        # by its status, its closed reason code ("unrecorded" for a row from
        # before reason codes, or a sent one) and Meta's error code if any.
        breakdown = await self.session.execute(
            select(
                CampaignRecipient.status,
                CampaignRecipient.reason_code,
                Message.failure_code,
                func.count(),
            )
            .select_from(CampaignRecipient)
            .outerjoin(Message, Message.id == CampaignRecipient.message_id)
            .where(scope)
            .group_by(CampaignRecipient.status, CampaignRecipient.reason_code, Message.failure_code)
            .order_by(
                CampaignRecipient.status,
                CampaignRecipient.reason_code,
                Message.failure_code,
            )
        )
        reasons = tuple(
            (status.value, reason or UNRECORDED_REASON, code, int(count))
            for status, reason, code, count in breakdown.all()
        )

        return CampaignStatistics(
            held=int(held or 0),
            pending=tally.get(RecipientStatus.PENDING, 0),
            sent=tally.get(RecipientStatus.SENT, 0),
            failed=tally.get(RecipientStatus.FAILED, 0),
            skipped=tally.get(RecipientStatus.SKIPPED, 0),
            # A read message was delivered. Meta reports the states in order and
            # a message only ever carries the furthest one it reached, so
            # counting `delivered` alone would undercount by everyone who read it.
            delivered=by_message.get(MessageStatus.DELIVERED, 0) + read,
            read=read,
            failed_after_send=int(failed_after_send or 0),
            replied=replied,
            opted_out_after=opted_out_after,
            reasons=reasons,
        )

    async def progress(self, campaign_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
        """Recipient counts per campaign in one grouped query (PG-12)."""
        rows = await self.session.execute(
            select(
                CampaignRecipient.campaign_id,
                CampaignRecipient.status,
                func.count(),
                func.count(CampaignRecipient.replied_at),
            )
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id.in_(list(campaign_ids)),
            )
            .group_by(CampaignRecipient.campaign_id, CampaignRecipient.status)
        )
        counts: dict[uuid.UUID, dict[str, int]] = {
            campaign_id: {
                "sent": 0,
                "failed": 0,
                "skipped": 0,
                "pending": 0,
                "replied": 0,
                "delivered": 0,
                "read": 0,
            }
            for campaign_id in campaign_ids
        }
        for campaign_id, status, count, answered in rows.all():
            counts[campaign_id][status.value] = int(count)
            counts[campaign_id]["replied"] += int(answered)
        delivery = await self.session.execute(
            select(CampaignRecipient.campaign_id, Message.status, func.count())
            .join(Message, Message.id == CampaignRecipient.message_id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id.in_(list(campaign_ids)),
                Message.status.in_([MessageStatus.DELIVERED, MessageStatus.READ]),
            )
            .group_by(CampaignRecipient.campaign_id, Message.status)
        )
        for campaign_id, status, count in delivery.all():
            counts[campaign_id]["delivered"] += int(count)
            if status is MessageStatus.READ:
                counts[campaign_id]["read"] += int(count)
        return counts

    async def export_rows(
        self,
        campaign_id: uuid.UUID,
        *,
        status: RecipientStatus | None,
        reason: str | None,
        after: uuid.UUID | None,
        limit: int,
    ) -> list[ExportRow]:
        """One page of the CSV export, by recipient id (PG-14)."""
        statement = (
            select(
                CampaignRecipient.id,
                Contact.display_name,
                Contact.wa_id,
                CampaignRecipient.status,
                CampaignRecipient.reason_code,
                Message.failure_code,
                CampaignRecipient.sent_at,
                Message.delivered_at,
                Message.read_at,
                CampaignRecipient.replied_at,
                CampaignRecipient.opted_out_after_at,
            )
            .select_from(CampaignRecipient)
            .join(
                Contact,
                and_(
                    Contact.id == CampaignRecipient.contact_id,
                    Contact.tenant_id == self.tenant_id,
                ),
            )
            .outerjoin(Message, Message.id == CampaignRecipient.message_id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
        )
        if status is not None:
            statement = statement.where(CampaignRecipient.status == status)
        if reason is not None:
            statement = statement.where(CampaignRecipient.reason_code == reason)
        if after is not None:
            statement = statement.where(CampaignRecipient.id > after)
        rows = await self.session.execute(statement.order_by(CampaignRecipient.id).limit(limit))
        return [ExportRow(*row) for row in rows.all()]


class AudienceRepository(TenantScopedRepository[Contact]):
    """Who a campaign of this workspace is allowed to reach.

    The base population is the rule, and it is written once, here: contacts with
    an existing conversation on the sending number who have not opted out on
    its channel (ENT-19). There is no route that uploads a list of phone
    numbers, and this is why — a campaign can only reach someone who chose to
    write to this business.
    """

    model = Contact

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Contact.tenant_id == self.tenant_id

    def _eligible(
        self,
        *,
        account_id: uuid.UUID,
        filters: AudienceFilter,
    ) -> Select[tuple[Contact]]:
        query = (
            self._select()
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.account_id == account_id,
                # The opt-out check, on the conversation's channel - the
                # sending connection's. Not a filter a caller can turn off: it
                # is part of the base population.
                ~refused(self.tenant_id, Contact.id, Conversation.channel),
            )
        )

        if filters.nobody:
            query = query.where(false())
        if filters.contact_ids:
            query = query.where(Contact.id.in_(filters.contact_ids))

        if filters.last_inbound_within_days is not None:
            since = datetime.now(UTC) - timedelta(days=filters.last_inbound_within_days)
            query = query.where(Conversation.last_inbound_at >= since)

        if filters.lead_statuses:
            query = query.where(
                select(Lead.id)
                .where(
                    Lead.tenant_id == self.tenant_id,
                    Lead.contact_id == Contact.id,
                    Lead.status.in_(filters.lead_statuses),
                )
                .exists()
            )

        return query.distinct()

    async def list_eligible_members(
        self,
        *,
        account_id: uuid.UUID,
        filters: AudienceFilter,
        limit: int,
    ) -> list[tuple[Contact, uuid.UUID]]:
        """Eligible contacts, each with the identity its conversation on this number addresses.

        The identity is what a copy is sent to (OMNI-004): recorded on the
        recipient when the audience is materialised, so "who exactly was this
        sent to" is answerable from the row, and a contact holding a phone and a
        business-scoped id is never addressed by whichever a send happened to
        pick. One conversation per contact per number, so one pair per contact.
        """
        statement = (
            self._eligible(account_id=account_id, filters=filters)
            .add_columns(Conversation.participant_identity_id)
            .order_by(Contact.id)
            .limit(limit)
        )
        rows = await self.session.execute(statement)
        return [(contact, participant) for contact, participant in rows.all()]

    async def count_eligible(self, *, account_id: uuid.UUID, filters: AudienceFilter) -> int:
        subquery = self._eligible(account_id=account_id, filters=filters).subquery()
        result = await self.session.execute(select(func.count()).select_from(subquery))
        return int(result.scalar_one())


class DueCampaignClaim(BaseRepository[Campaign]):
    """Claims campaigns whose moment has come, across every workspace.

    **Deliberately not tenant-scoped**, and one of only two classes in the
    codebase that is not. The worker is a platform process on a timer; there is
    no authenticated tenant for it to be confined to. Nothing here is reachable
    from a request, and every row it returns is handed to a tenant-scoped
    service keyed on that row's own `tenant_id`.
    """

    model = Campaign

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def claim_by_id(
        self,
        campaign_id: uuid.UUID,
        *,
        lease_token: uuid.UUID | None = None,
        lease_until: datetime | None = None,
    ) -> Campaign | None:
        """Re-take one claimed campaign in a transaction of its own.

        The batch claim below hands back identifiers rather than rows to work
        with, because its transaction has committed by the time any batch is
        sent and an object from it is a snapshot. This reads the row again under
        its own lock, so a campaign paused in between is seen as paused.

        **Only while the claim is still the caller's** (CMP-01). With a
        `lease_token`, the row must still carry it: a campaign whose lease ran
        out while this worker was busy with an earlier campaign of its sweep,
        and which another replica has claimed since, answers None here and is
        left to that replica. `lease_until` renews the lease from this moment,
        because the claim that set it may have been minutes ago.
        """
        statement = select(Campaign).where(Campaign.id == campaign_id)
        if lease_token is not None:
            statement = statement.where(Campaign.lease_token == lease_token)
        campaign = (
            (
                await self.session.execute(
                    statement.with_for_update(skip_locked=True).execution_options(
                        populate_existing=True
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if campaign is not None and lease_until is not None:
            campaign.next_send_at = lease_until
        return campaign

    async def stuck(self, *, before: datetime, now: datetime) -> tuple[int, float]:
        """Live campaigns overdue since `before`, and the longest wait in seconds (CMP-23)."""
        due = func.coalesce(Campaign.next_send_at, Campaign.scheduled_at)
        row = (
            await self.session.execute(
                select(func.count(), func.min(due)).where(
                    Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
                    due.is_not(None),
                    due < before,
                )
            )
        ).one()
        count, earliest = int(row[0] or 0), row[1]
        return count, max((now - earliest).total_seconds(), 0.0) if earliest else 0.0

    async def record_batch_failure(
        self,
        campaign_id: uuid.UUID,
        *,
        lease_token: uuid.UUID,
        detail: str,
        pause_after: int,
        reason: str,
    ) -> Campaign | None:
        """Count one batch that raised; pause the campaign once `pause_after` have in a row.

        One conditional statement, only while the claim is still the caller's
        (CMP-20). Returns the campaign when this failure paused it.
        """
        counted = (
            await self.session.execute(
                update(Campaign)
                .where(
                    Campaign.id == campaign_id,
                    Campaign.lease_token == lease_token,
                    Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
                )
                .values(
                    consecutive_batch_failures=Campaign.consecutive_batch_failures + 1,
                    last_error=detail,
                )
                .returning(Campaign.consecutive_batch_failures)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if counted is None or counted < pause_after:
            return None
        return (
            (
                await self.session.execute(
                    update(Campaign)
                    .where(Campaign.id == campaign_id, Campaign.lease_token == lease_token)
                    .values(
                        status=CampaignStatus.PAUSED,
                        status_reason=reason,
                        next_send_at=None,
                        lease_token=None,
                    )
                    .returning(Campaign)
                    .execution_options(synchronize_session=False)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_CAMPAIGN_CLAIM_LIMIT,
        lease_until: datetime | None = None,
    ) -> list[Campaign]:
        """Lock and return campaigns that should be sending.

        Two kinds qualify: a scheduled campaign whose time has arrived, and a
        running one whose rate limit has expired. They are claimed by the same
        query because the worker does the same thing with both — send the next
        batch — and splitting them would be two sweeps racing each other for the
        same rows.

        `lease_until` pushes `next_send_at` out and is committed with the claim,
        which is what keeps the guarantee once the lock is gone (ADR-093). It is
        the same column the query above filters on, so the lease is the rate
        limiter's own mechanism applied a moment earlier rather than a new one.

        **Both kinds honour the lease** (CMP-01). A scheduled campaign stays
        `scheduled` until its first send commits, so a branch that matched it
        on `scheduled_at` alone handed a campaign another replica had just
        leased to a second one. `schedule()` clears `next_send_at`, so a newly
        scheduled campaign is due at once, as before.

        Each claimed row gets a fresh `lease_token`, which the worker carries
        to `claim_by_id` and to every recipient: a lease that runs out and is
        re-taken changes the token, and the earlier holder stops.
        """
        statement = (
            select(Campaign)
            .where(
                or_(
                    and_(
                        Campaign.status == CampaignStatus.SCHEDULED,
                        Campaign.scheduled_at.is_not(None),
                        Campaign.scheduled_at <= now,
                    ),
                    Campaign.status == CampaignStatus.RUNNING,
                ),
                or_(
                    Campaign.next_send_at.is_(None),
                    Campaign.next_send_at <= now,
                ),
            )
            .order_by(Campaign.scheduled_at.nulls_last(), Campaign.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        claimed = list(result.scalars().all())
        if lease_until is not None:
            for row in claimed:
                if row.lease_token is not None:
                    # A batch ends by clearing its token, so one still set is
                    # a lease that ran out under a worker that stopped, or
                    # stalled, mid-batch. Counted: in normal operation it is
                    # a crash, and anything more is a lease too short.
                    logger.warning(
                        "campaign.lease_taken_over",
                        extra={"event": "campaign.lease_taken_over", "campaign_id": str(row.id)},
                    )
                    await record_campaign_event("lease_taken_over")
                row.next_send_at = lease_until
                row.lease_token = uuid.uuid4()
            await self.session.flush()
        return claimed


__all__ = [
    "DEFAULT_CAMPAIGN_CLAIM_LIMIT",
    "DEFAULT_RECIPIENT_BATCH",
    "AudienceFilter",
    "AudienceRepository",
    "CampaignRecipientRepository",
    "CampaignRepository",
    "CampaignStatistics",
    "DueCampaignClaim",
]
