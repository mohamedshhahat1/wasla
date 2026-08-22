"""Reporting queries over the domain tables.

Every figure here is computed from rows that already exist - messages,
conversations, leads, sentiment readings, campaign recipients - rather than from
a parallel event stream (ADR-028). That is what makes a metric defined next
month computable for last month.

Two definitions are load-bearing and are written down here because a number
whose definition is unwritten is a number two people will read differently:

**Average response time** is the time from a customer message that *started a
burst* to the next business message in that conversation. A burst is a message
whose predecessor was not also inbound: a customer sending four messages in a
row waited once, not four times, and counting each of them would divide the
same wait by four and flatter the figure. A customer still waiting for a first
reply contributes nothing rather than an infinity, and is counted separately as
`unanswered`.

**AI resolution rate** is the share of conversations *created in the window*
that were never handed to a person. Conversations rather than handoff events,
because one conversation bounced between agent and colleague three times is one
conversation that the AI did not resolve.

The window is half-open, `[since, until)`, as everywhere else in this phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from app.db.models.lead import Lead, LeadStatus
from app.db.models.sentiment import MessageSentiment, SentimentLabel

# Statuses that mean a customer's opportunity has been qualified or better.
# Written out rather than derived from declaration order, for the reason
# `SENTIMENT_SEVERITY` is: reordering the enum for an unrelated reason must not
# silently change what "qualified" counts.
QUALIFIED_STATUSES: frozenset[LeadStatus] = frozenset(
    {
        LeadStatus.QUALIFIED,
        LeadStatus.PROPOSAL,
        LeadStatus.WON,
    }
)

# How a customer sounds when a business would want to know about it.
UNHAPPY_LABELS: frozenset[SentimentLabel] = frozenset(
    {
        SentimentLabel.NEGATIVE,
        SentimentLabel.ANGRY,
    }
)


@dataclass(frozen=True, slots=True)
class ConversationMetrics:
    """Volume and outcome for the conversations opened in a window."""

    created: int = 0
    handed_off: int = 0
    escalated: int = 0

    @property
    def ai_resolved(self) -> int:
        """Conversations opened in the window that no person had to take."""
        return max(self.created - self.handed_off, 0)

    @property
    def ai_resolution_rate(self) -> float:
        """Share the agent handled alone, 0 to 1.

        Zero conversations is reported as 0.0 rather than as an error or a
        division by zero. A workspace with no traffic resolved nothing, which is
        the honest reading and the one a chart can draw.
        """
        if self.created == 0:
            return 0.0
        return round(self.ai_resolved / self.created, 4)


@dataclass(frozen=True, slots=True)
class MessageMetrics:
    """Traffic in both directions, and how long customers waited."""

    received: int = 0
    sent: int = 0
    failed: int = 0
    average_response_seconds: float | None = None
    unanswered: int = 0


@dataclass(frozen=True, slots=True)
class LeadMetrics:
    """Pipeline created in a window, and what became of it."""

    created: int = 0
    qualified: int = 0
    won: int = 0
    lost: int = 0
    by_status: dict[LeadStatus, int] = field(default_factory=dict)

    @property
    def conversion_rate(self) -> float:
        """Won as a share of leads created in the window, 0 to 1.

        Deliberately naive, and documented as such: a lead created in August and
        won in September counts in neither month's rate. Cohort accounting is a
        product decision, not an arithmetic one.
        """
        if self.created == 0:
            return 0.0
        return round(self.won / self.created, 4)


@dataclass(frozen=True, slots=True)
class SentimentMetrics:
    """How customers sounded."""

    readings: int = 0
    unhappy_conversations: int = 0
    by_label: dict[SentimentLabel, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CampaignMetrics:
    """What broadcasts did in a window.

    `delivered` is read from the message rows rather than from the recipient,
    because delivery is Meta's word and arrives later as a status webhook. A
    recipient is `sent` the moment the request succeeds; whether it was
    delivered is a different question with a different answer.
    """

    sent: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0


class TenantMetricsRepository:
    """Reporting reads for one workspace.

    Not a `TenantScopedRepository`: it spans several models rather than owning
    one, so it applies the tenant predicate to each query itself. The tenant id
    is fixed at construction from the authenticated context, exactly as it is
    for a scoped repository, and no method takes one.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id

    async def conversations(self, *, since: datetime, until: datetime) -> ConversationMetrics:
        created = await self._session.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.tenant_id == self._tenant_id)
            .where(Conversation.created_at >= since)
            .where(Conversation.created_at < until)
        )

        # Distinct conversations, not handoff events. One conversation bounced
        # between agent and colleague three times is one conversation the AI did
        # not resolve; counting events would subtract three from the same
        # denominator and can drive the rate negative on a busy day.
        handed_off = await self._session.scalar(
            select(func.count(func.distinct(AnalyticsEvent.conversation_id)))
            .select_from(AnalyticsEvent)
            .join(Conversation, Conversation.id == AnalyticsEvent.conversation_id)
            .where(AnalyticsEvent.tenant_id == self._tenant_id)
            .where(AnalyticsEvent.event_type == AnalyticsEventType.HANDOFF)
            # The *conversation* is in the window, not the handoff: the figure
            # answers "of the conversations we opened, how many did we finish",
            # and a conversation opened yesterday and escalated today belongs to
            # yesterday's cohort.
            .where(Conversation.created_at >= since)
            .where(Conversation.created_at < until)
        )

        escalated = await self._session.scalar(
            select(func.count(func.distinct(MessageSentiment.conversation_id)))
            .select_from(MessageSentiment)
            .where(MessageSentiment.tenant_id == self._tenant_id)
            .where(MessageSentiment.escalated.is_(True))
            .where(MessageSentiment.created_at >= since)
            .where(MessageSentiment.created_at < until)
        )

        return ConversationMetrics(
            created=int(created or 0),
            handed_off=int(handed_off or 0),
            escalated=int(escalated or 0),
        )

    async def messages(self, *, since: datetime, until: datetime) -> MessageMetrics:
        """Traffic and waiting time, in three queries."""
        rows = await self._session.execute(
            select(Message.direction, Message.status, func.count())
            .where(Message.tenant_id == self._tenant_id)
            .where(Message.created_at >= since)
            .where(Message.created_at < until)
            .group_by(Message.direction, Message.status)
        )
        received = sent = failed = 0
        for direction, status, count in rows.all():
            if direction is MessageDirection.INBOUND:
                received += int(count)
            elif status is MessageStatus.FAILED:
                # A message that never left is not traffic the business sent.
                failed += int(count)
            else:
                sent += int(count)

        average, unanswered = await self._response_time(since=since, until=until)
        return MessageMetrics(
            received=received,
            sent=sent,
            failed=failed,
            average_response_seconds=average,
            unanswered=unanswered,
        )

    async def _response_time(
        self,
        *,
        since: datetime,
        until: datetime,
    ) -> tuple[float | None, int]:
        """Mean seconds to the first reply, and how many are still waiting.

        The lateral join finds each burst-opening customer message's first
        business reply. A `LEFT OUTER` join rather than an inner one, so the
        conversations nobody has answered are visible as a count instead of
        silently improving the average by being absent.
        """
        inbound = aliased(Message, name="customer_message")
        previous = aliased(Message, name="preceding_message")
        outbound = aliased(Message, name="business_reply")

        # The message immediately before this one in the same conversation.
        # A customer sending four messages in a row waited once, not four
        # times, so only the first of a run is measured.
        preceding = (
            select(previous.direction)
            .where(previous.conversation_id == inbound.conversation_id)
            .where(previous.created_at < inbound.created_at)
            .order_by(previous.created_at.desc(), previous.id.desc())
            .limit(1)
            .correlate(inbound)
            .scalar_subquery()
        )

        reply = (
            select(outbound.created_at.label("replied_at"))
            .where(outbound.conversation_id == inbound.conversation_id)
            .where(outbound.direction == MessageDirection.OUTBOUND)
            .where(outbound.status != MessageStatus.FAILED)
            .where(outbound.created_at > inbound.created_at)
            .order_by(outbound.created_at.asc(), outbound.id.asc())
            .limit(1)
            .correlate(inbound)
            .lateral("first_reply")
        )

        statement = (
            select(
                func.avg(
                    func.extract("epoch", reply.c.replied_at - inbound.created_at),
                ),
                func.count().filter(reply.c.replied_at.is_(None)),
            )
            .select_from(inbound)
            # `ON true`: the lateral subquery already correlates through its
            # own WHERE, so the join condition has nothing left to say.
            .outerjoin(reply, true())
            .where(inbound.tenant_id == self._tenant_id)
            .where(inbound.direction == MessageDirection.INBOUND)
            .where(inbound.created_at >= since)
            .where(inbound.created_at < until)
            .where(
                # Either nothing came before, or what did was ours.
                (preceding.is_(None))
                | (preceding != MessageDirection.INBOUND)
            )
        )

        row = (await self._session.execute(statement)).one()
        average = float(row[0]) if row[0] is not None else None
        return (round(average, 2) if average is not None else None, int(row[1] or 0))

    async def leads(self, *, since: datetime, until: datetime) -> LeadMetrics:
        rows = await self._session.execute(
            select(Lead.status, func.count())
            .where(Lead.tenant_id == self._tenant_id)
            .where(Lead.created_at >= since)
            .where(Lead.created_at < until)
            .group_by(Lead.status)
        )
        by_status = {status: int(count) for status, count in rows.all()}
        return LeadMetrics(
            created=sum(by_status.values()),
            qualified=sum(
                count for status, count in by_status.items() if status in QUALIFIED_STATUSES
            ),
            won=by_status.get(LeadStatus.WON, 0),
            lost=by_status.get(LeadStatus.LOST, 0),
            by_status=by_status,
        )

    async def sentiment(self, *, since: datetime, until: datetime) -> SentimentMetrics:
        rows = await self._session.execute(
            select(MessageSentiment.label, func.count())
            .where(MessageSentiment.tenant_id == self._tenant_id)
            .where(MessageSentiment.created_at >= since)
            .where(MessageSentiment.created_at < until)
            .group_by(MessageSentiment.label)
        )
        by_label = {label: int(count) for label, count in rows.all()}

        # Conversations rather than readings: a customer who complains six times
        # is one unhappy customer, and an inbox counts people.
        unhappy = await self._session.scalar(
            select(func.count(func.distinct(MessageSentiment.conversation_id)))
            .where(MessageSentiment.tenant_id == self._tenant_id)
            .where(MessageSentiment.label.in_(list(UNHAPPY_LABELS)))
            .where(MessageSentiment.created_at >= since)
            .where(MessageSentiment.created_at < until)
        )
        return SentimentMetrics(
            readings=sum(by_label.values()),
            unhappy_conversations=int(unhappy or 0),
            by_label=by_label,
        )

    async def campaigns(self, *, since: datetime, until: datetime) -> CampaignMetrics:
        rows = await self._session.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.tenant_id == self._tenant_id)
            .where(CampaignRecipient.created_at >= since)
            .where(CampaignRecipient.created_at < until)
            .group_by(CampaignRecipient.status)
        )
        by_status = {status: int(count) for status, count in rows.all()}

        # Delivery is Meta's word, and it arrives later as a status webhook.
        # Read from the message rather than the recipient, which only knows the
        # send was accepted.
        delivered = await self._session.scalar(
            select(func.count())
            .select_from(CampaignRecipient)
            .join(Message, Message.id == CampaignRecipient.message_id)
            .where(CampaignRecipient.tenant_id == self._tenant_id)
            .where(CampaignRecipient.created_at >= since)
            .where(CampaignRecipient.created_at < until)
            .where(
                Message.status.in_(
                    [
                        MessageStatus.DELIVERED,
                        MessageStatus.READ,
                    ]
                )
            )
        )
        return CampaignMetrics(
            sent=by_status.get(RecipientStatus.SENT, 0),
            delivered=int(delivered or 0),
            failed=by_status.get(RecipientStatus.FAILED, 0),
            skipped=by_status.get(RecipientStatus.SKIPPED, 0),
        )
