"""What customers did after a campaign reached them: replied, opted out (PG-10, ADR-149).

**A reply** is an inbound message from a recipient's contact on the
campaign's number within `CAMPAIGN_REPLY_ATTRIBUTION_SECONDS` (72 hours) of
that recipient's send being accepted. It is recorded once, at ingestion, in
the transaction that stores the inbound message, by one bounded indexed
UPDATE - on the **most recent** campaign recipient of that contact on that
number inside the window: a reply answers the last thing a customer received,
and crediting every campaign of the last three days would count one answer
several times.

**An opt-out after** is a customer's own refusal (any source but a
colleague's) recorded within the same window after a send - on the most recent
campaign recipient of that contact, on any number of the channel, since an
opt-out covers all of them (ENT-19). A STOP is both: the message is a reply,
and what it asks for is an opt-out.

Neither moves once set: a second reply is still one reply.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import ScalarSelect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.campaign import Campaign, CampaignRecipient


def attribution_window() -> timedelta:
    return timedelta(seconds=get_settings().campaign_reply_attribution_seconds)


def _latest_sent(
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    at: datetime,
    window: timedelta,
    account_id: uuid.UUID | None,
) -> ScalarSelect[uuid.UUID]:
    statement = (
        select(CampaignRecipient.id)
        .where(
            CampaignRecipient.tenant_id == tenant_id,
            CampaignRecipient.contact_id == contact_id,
            CampaignRecipient.sent_at.is_not(None),
            CampaignRecipient.sent_at <= at,
            CampaignRecipient.sent_at > at - window,
        )
        .order_by(CampaignRecipient.sent_at.desc(), CampaignRecipient.id.desc())
        .limit(1)
    )
    if account_id is not None:
        statement = statement.where(
            CampaignRecipient.campaign_id.in_(
                select(Campaign.id).where(
                    Campaign.tenant_id == tenant_id, Campaign.account_id == account_id
                )
            )
        )
    return statement.scalar_subquery()


async def attribute_reply(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    account_id: uuid.UUID,
    at: datetime,
    window: timedelta | None = None,
) -> bool:
    """Mark the contact's latest campaign copy on this number as replied; whether one was."""
    latest = _latest_sent(
        tenant_id=tenant_id,
        contact_id=contact_id,
        at=at,
        window=window or attribution_window(),
        account_id=account_id,
    )
    result = await session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.id == latest,
            CampaignRecipient.replied_at.is_(None),
        )
        .values(replied_at=at)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def attribute_opt_out(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    at: datetime,
    window: timedelta | None = None,
) -> bool:
    """Mark the contact's latest campaign copy as followed by an opt-out; whether one was."""
    latest = _latest_sent(
        tenant_id=tenant_id,
        contact_id=contact_id,
        at=at,
        window=window or attribution_window(),
        account_id=None,
    )
    result = await session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.id == latest,
            CampaignRecipient.opted_out_after_at.is_(None),
        )
        .values(opted_out_after_at=at)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


__all__ = ["attribute_opt_out", "attribute_reply", "attribution_window"]
