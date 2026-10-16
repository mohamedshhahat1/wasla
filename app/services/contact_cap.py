"""One marketing message per customer per 24 hours, per workspace (FREQ-01, ADR-147; CMQ-03).

**What counts.** Every marketing template a workspace sends a contact - from
any campaign, or by hand. Not a test send, not a utility or authentication
template, not a follow-up, not an AI or human reply.

**Claimed in one statement.** `UPDATE contacts SET last_marketing_sent_at = at
WHERE id = ... AND (last_marketing_sent_at IS NULL OR last_marketing_sent_at <=
at - interval)`: under PostgreSQL's row lock a concurrent claim re-reads the
row and finds the condition false, so two campaigns on two workers can never
both win the same 24 hours. It runs in the transaction that commits the send
intent (ADR-093 TX1) - the one that took the campaign recipient - and nothing
is staged when it loses.

**Given back only when provably undelivered.** A send Meta refused restores
the value the claim replaced, if nothing has claimed since; an uncertain one
keeps it. Meta's own per-user marketing limit (131049) pushes the claim to
now: "wait at least 24 hours before resending" (per-user marketing limits,
developers.facebook.com, read 2026-10-16).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.db.models.conversation import Contact, Message

#: The stable error code a manual marketing template inside the cap meets.
CONTACT_FREQUENCY_CAP_CODE: Final = "contact_frequency_cap"


class ContactFrequencyCapError(ConflictError):
    """This contact was sent a marketing message less than 24 hours ago (409)."""

    error_code = CONTACT_FREQUENCY_CAP_CODE

    def __init__(self, *, available_at: datetime) -> None:
        super().__init__(
            "This customer was sent a marketing message in the last 24 hours. "
            "The next one can be sent after the time in the details.",
            error_code=CONTACT_FREQUENCY_CAP_CODE,
            details={"available_at": available_at.isoformat()},
        )
        self.available_at = available_at


@dataclass(frozen=True, slots=True)
class Claim:
    claimed_at: datetime
    previous: datetime | None


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    at: datetime,
    interval: timedelta,
) -> Claim:
    """Take this contact's marketing slot for the next `interval`, or raise.

    One conditional statement. `previous` is what it replaced, read in the
    same statement, for `release`.
    """
    previous = (
        select(Contact.last_marketing_sent_at)
        .where(Contact.id == contact_id, Contact.tenant_id == tenant_id)
        .scalar_subquery()
    )
    won = (
        await session.execute(
            update(Contact)
            .where(
                Contact.id == contact_id,
                Contact.tenant_id == tenant_id,
                or_(
                    Contact.last_marketing_sent_at.is_(None),
                    Contact.last_marketing_sent_at <= at - interval,
                ),
            )
            .values(last_marketing_sent_at=at)
            .returning(previous)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if won is not None:
        return Claim(claimed_at=at, previous=won[0])
    current = await session.scalar(
        select(Contact.last_marketing_sent_at).where(
            Contact.id == contact_id, Contact.tenant_id == tenant_id
        )
    )
    raise ContactFrequencyCapError(available_at=(current or at) + interval)


async def release(session: AsyncSession, message: Message, *, contact_id: uuid.UUID) -> bool:
    """Give back the claim a provably undelivered send made, if nobody claimed since."""
    if message.marketing_cap_claimed_at is None:
        return False
    result = await session.execute(
        update(Contact)
        .where(
            Contact.id == contact_id,
            Contact.tenant_id == message.tenant_id,
            Contact.last_marketing_sent_at == message.marketing_cap_claimed_at,
        )
        .values(last_marketing_sent_at=message.marketing_cap_previous_at)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def push(
    session: AsyncSession, *, tenant_id: uuid.UUID, contact_id: uuid.UUID, at: datetime
) -> None:
    """Meta's per-user marketing limit (131049): no marketing to this person for 24 hours."""
    await session.execute(
        update(Contact)
        .where(
            Contact.id == contact_id,
            Contact.tenant_id == tenant_id,
            or_(
                Contact.last_marketing_sent_at.is_(None),
                and_(Contact.last_marketing_sent_at < at),
            ),
        )
        .values(last_marketing_sent_at=at)
        .execution_options(synchronize_session=False)
    )


__all__ = [
    "CONTACT_FREQUENCY_CAP_CODE",
    "Claim",
    "ContactFrequencyCapError",
    "claim",
    "push",
    "release",
]
