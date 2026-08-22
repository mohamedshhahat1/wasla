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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.db.models.conversation import Contact, Conversation, Message, MessageStatus
from app.db.models.lead import Lead, LeadStatus
from app.repositories.base import BaseRepository, TenantScopedRepository

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

    def as_dict(self) -> dict[str, Any]:
        """The record kept on the campaign, so a person can read what was targeted."""
        return {
            "last_inbound_within_days": self.last_inbound_within_days,
            "lead_statuses": [status.value for status in self.lead_statuses],
            "contact_ids": [str(contact_id) for contact_id in self.contact_ids],
        }


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

    async def require_by_id(self, campaign_id: uuid.UUID) -> Campaign:
        return await self._require(self._select().where(Campaign.id == campaign_id))

    async def list_campaigns(
        self,
        *,
        statuses: tuple[CampaignStatus, ...] = (),
        account_id: uuid.UUID | None = None,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[Campaign]:
        """Newest first, paged by keyset."""
        query = self._select()
        if statuses:
            query = query.where(Campaign.status.in_(statuses))
        if account_id is not None:
            query = query.where(Campaign.account_id == account_id)
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
        limit: int = 100,
    ) -> list[CampaignRecipient]:
        query = self._select().where(CampaignRecipient.campaign_id == campaign_id)
        if status is not None:
            query = query.where(CampaignRecipient.status == status)
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
    ) -> CampaignRecipient:
        return self.add(
            CampaignRecipient(
                tenant_id=self.tenant_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                status=RecipientStatus.PENDING,
                attempts=0,
            )
        )

    async def claim_pending(
        self,
        campaign_id: uuid.UUID,
        *,
        limit: int = DEFAULT_RECIPIENT_BATCH,
    ) -> list[CampaignRecipient]:
        """Lock and return recipients still waiting for their copy.

        ``FOR UPDATE SKIP LOCKED`` for the reason follow-ups use it, with more
        at stake: two replicas working one campaign would otherwise both read
        the same pending rows and send ten thousand people the message twice.
        """
        statement = (
            select(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RecipientStatus.PENDING,
            )
            .order_by(CampaignRecipient.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def statistics(self, campaign_id: uuid.UUID) -> CampaignStatistics:
        """Counts per outcome, plus what Meta has since said about delivery.

        Two queries rather than one. The outcome counts come from the recipient
        rows; delivery and read come from the message rows a webhook advances,
        and joining them into a single grouped query would double-count the
        recipients that have no message yet.
        """
        counts = await self.session.execute(
            select(CampaignRecipient.status, func.count())
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
            .group_by(CampaignRecipient.status)
        )
        tally = {status: int(count) for status, count in counts.all()}

        delivery = await self.session.execute(
            select(Message.status, func.count())
            .join(CampaignRecipient, CampaignRecipient.message_id == Message.id)
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign_id,
            )
            .group_by(Message.status)
        )
        by_message = {status: int(count) for status, count in delivery.all()}
        read = by_message.get(MessageStatus.READ, 0)

        return CampaignStatistics(
            pending=tally.get(RecipientStatus.PENDING, 0),
            sent=tally.get(RecipientStatus.SENT, 0),
            failed=tally.get(RecipientStatus.FAILED, 0),
            skipped=tally.get(RecipientStatus.SKIPPED, 0),
            # A read message was delivered. Meta reports the states in order and
            # a message only ever carries the furthest one it reached, so
            # counting `delivered` alone would undercount by everyone who read it.
            delivered=by_message.get(MessageStatus.DELIVERED, 0) + read,
            read=read,
        )


class AudienceRepository(TenantScopedRepository[Contact]):
    """Who a campaign of this workspace is allowed to reach.

    The base population is the rule, and it is written once, here: contacts with
    an existing conversation on the sending number who have not opted out. There
    is no route that uploads a list of phone numbers, and this is why — a
    campaign can only reach someone who chose to write to this business.
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
                # The opt-out check. Not a filter a caller can turn off: it is
                # part of the base population.
                Contact.marketing_opt_out_at.is_(None),
            )
        )

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

    async def list_eligible(
        self,
        *,
        account_id: uuid.UUID,
        filters: AudienceFilter,
        limit: int,
    ) -> list[Contact]:
        return await self._all(
            self._eligible(account_id=account_id, filters=filters).order_by(Contact.id).limit(limit)
        )

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

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_CAMPAIGN_CLAIM_LIMIT,
    ) -> list[Campaign]:
        """Lock and return campaigns that should be sending.

        Two kinds qualify: a scheduled campaign whose time has arrived, and a
        running one whose rate limit has expired. They are claimed by the same
        query because the worker does the same thing with both — send the next
        batch — and splitting them would be two sweeps racing each other for the
        same rows.
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
                    and_(
                        Campaign.status == CampaignStatus.RUNNING,
                        or_(
                            Campaign.next_send_at.is_(None),
                            Campaign.next_send_at <= now,
                        ),
                    ),
                )
            )
            .order_by(Campaign.scheduled_at.nulls_last(), Campaign.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())


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
