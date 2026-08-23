"""Metering: staging what a workspace consumed, and reading it back.

Two classes with deliberately different shapes.

`UsageRecorder` is the write side. It is held by services that consume something
- the messaging service, the orchestrator's worker, the ingestion path - and it
never commits, never flushes and never queries. Staging a row costs nothing but
an `INSERT` in the transaction that is already open, which is what makes it safe
to call from inside a hot path.

`UsageService` is the read side, and every method on it is an aggregate. Nobody
wants usage rows; they want a figure for a window.

A recorder failure must never lose the work it was measuring. Staging cannot
fail on its own - `session.add` performs no I/O - so there is no try/except
here pretending otherwise. What can fail is the flush at commit time, and that
takes the whole unit of work with it by design: a message that was sent and not
counted is a bill that is quietly wrong, and the transaction is exactly the tool
for refusing that outcome.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models.usage import UsageEvent, UsageEventType
from app.repositories.usage_repository import (
    UsageEventRepository,
    UsagePoint,
    UsageTotal,
)

# What a request gets when it names neither end of the window.
DEFAULT_WINDOW: Final = timedelta(days=30)
# The widest window a single request may aggregate. Not a security boundary -
# the index makes a year cheap - but an unbounded range on a table that grows
# forever is a query nobody meant to run.
MAX_WINDOW: Final = timedelta(days=366)


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """The half-open window `[since, until)` a summary covers."""

    since: datetime
    until: datetime


def resolve_window(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> UsageWindow:
    """Turn optional bounds into a window, or refuse them.

    Both ends are normalised to UTC. A naive datetime is read as UTC rather than
    rejected: the API declares its times in UTC, and refusing a value that is
    unambiguous in context is pedantry a caller cannot act on.
    """
    moment = now if now is not None else datetime.now(UTC)
    end = _as_utc(until) if until is not None else moment
    start = _as_utc(since) if since is not None else end - DEFAULT_WINDOW

    if start >= end:
        raise ValidationError("The start of the window must be before its end.")
    if end - start > MAX_WINDOW:
        raise ValidationError("A usage window may not be longer than 366 days.")
    return UsageWindow(since=start, until=end)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """The named counters a dashboard and a bill are both drawn from.

    Named fields rather than a bare list of totals, because the consumers of
    this are a plan limit and a chart axis, and both want to ask for one number
    without knowing the enum. `totals` carries the unabridged version, so a
    meter added later is visible before anything here is changed to name it.
    """

    window: UsageWindow
    messages_received: int = 0
    messages_sent: int = 0
    ai_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rag_queries: int = 0
    media_processed: int = 0
    voice_transcriptions: int = 0
    storage_bytes: int = 0
    leads_created: int = 0
    conversations_created: int = 0
    campaign_messages: int = 0
    api_requests: int = 0
    totals: tuple[UsageTotal, ...] = ()

    @property
    def total_tokens(self) -> int:
        """Input plus output. Derived, never stored: two sums that disagree with
        their own total is a support ticket nobody can answer."""
        return self.input_tokens + self.output_tokens


# Which named counter each meter fills. A meter absent from this map still
# appears in `totals`; it simply has no shorthand yet.
_SUMMARY_FIELDS: Final[dict[UsageEventType, str]] = {
    UsageEventType.WHATSAPP_MESSAGE_RECEIVED: "messages_received",
    UsageEventType.WHATSAPP_MESSAGE_SENT: "messages_sent",
    UsageEventType.AI_REQUEST: "ai_requests",
    UsageEventType.AI_INPUT_TOKEN: "input_tokens",
    UsageEventType.AI_OUTPUT_TOKEN: "output_tokens",
    UsageEventType.RAG_QUERY: "rag_queries",
    UsageEventType.MEDIA_PROCESSING: "media_processed",
    UsageEventType.VOICE_TRANSCRIPTION: "voice_transcriptions",
    UsageEventType.STORAGE_USED: "storage_bytes",
    UsageEventType.LEAD_CREATED: "leads_created",
    UsageEventType.CONVERSATION_CREATED: "conversations_created",
    UsageEventType.CAMPAIGN_MESSAGE: "campaign_messages",
    UsageEventType.API_REQUEST: "api_requests",
}


def summarise(totals: Iterable[UsageTotal], *, window: UsageWindow) -> UsageSummary:
    """Fold raw totals into the named counters."""
    collected = tuple(totals)
    values: dict[str, Any] = {
        _SUMMARY_FIELDS[total.event_type]: total.quantity
        for total in collected
        if total.event_type in _SUMMARY_FIELDS
    }
    return UsageSummary(window=window, totals=collected, **values)


class UsageRecorder:
    """Stages metered occurrences for one workspace.

    Every method is synchronous and returns nothing. A caller that has to await
    a meter is a caller who will eventually be tempted to skip one.
    """

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._events = UsageEventRepository(session, tenant_id=tenant_id)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._events.tenant_id

    def record(
        self,
        event_type: UsageEventType,
        *,
        quantity: int = 1,
        occurred_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> UsageEvent | None:
        """Stage one occurrence, unless there is nothing to count.

        A zero or negative quantity is dropped rather than stored. A model that
        reports no output tokens, a file of no bytes: those are rows that add
        nothing to any sum and noise to every scan. Negative is refused for a
        blunter reason - usage is append-only, so a negative row is the only way
        a total could ever go down, and that has to be a deliberate correction
        rather than a rounding accident.
        """
        if quantity <= 0:
            return None
        return self._events.record(
            event_type=event_type,
            quantity=quantity,
            occurred_at=occurred_at,
            meta=meta,
        )

    def ai_request(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        requests: int = 1,
        model: str | None = None,
        conversation_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Stage the request and its two token counts together.

        Three rows rather than one with two extra columns. Tokens are priced
        separately from requests and from each other, so each is its own meter;
        a single row would mean every sum in the system carrying a special case
        for which column to add.

        The model is recorded because a token from one model is not a token from
        another, and a bill that cannot say which was used cannot be checked.

        `requests` is how many provider calls the turn actually made. An agent
        that ran three tool rounds called the provider three times and is
        charged for three, while its tokens are the sum across all of them -
        counting the turn as one request would under-count the meter that
        every plan limit is written against.
        """
        meta: dict[str, Any] = {}
        if model is not None:
            meta["model"] = model
        if conversation_id is not None:
            meta["conversation_id"] = str(conversation_id)
        line = meta or None

        self.record(
            UsageEventType.AI_REQUEST,
            quantity=requests,
            occurred_at=occurred_at,
            meta=line,
        )
        self.record(
            UsageEventType.AI_INPUT_TOKEN,
            quantity=input_tokens,
            occurred_at=occurred_at,
            meta=line,
        )
        self.record(
            UsageEventType.AI_OUTPUT_TOKEN,
            quantity=output_tokens,
            occurred_at=occurred_at,
            meta=line,
        )


class UsageService:
    """Reads usage back for one workspace."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._events = UsageEventRepository(session, tenant_id=tenant_id)

    async def summary(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageSummary:
        """Every meter for a window, as named counters and as raw totals."""
        window = resolve_window(since=since, until=until)
        totals = await self._events.totals(since=window.since, until=window.until)
        return summarise(totals, window=window)

    async def series(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Iterable[UsageEventType] | None = None,
    ) -> tuple[UsageWindow, list[UsagePoint]]:
        """A daily point per meter, for drawing."""
        window = resolve_window(since=since, until=until)
        points = await self._events.daily(
            since=window.since,
            until=window.until,
            event_types=event_types,
        )
        return window, points
